"""The staged pipeline: contracts, the resumable state, briefs with arguments, and each stage
offline (a scripted model, a stub search, local pages)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import cast

import pytest
from pytest_httpserver import HTTPServer
from web.onboard.pipeline import (
    Brief,
    BriefError,
    Context,
    Onboarding,
    SearchResult,
    run,
)
from web.onboard.pipeline.stages.search import score_url
from web.resolve import Resolver

_BRIEF = """---
name: ir-news
title: Investor-relations news
args: [company]
search:
  term: "{company} investor relations press releases"
  k: 10
  domain: ["{company}", "investors.{company}", "ir.{company}", "q4cdn", "gcs-web"]
  path: ["news", "press", "release", "investor"]
look:
  - the company's OWN investor-relations news listing
ignore:
  - third-party wire copies, a single release page
expect_rows: "5-100"
schema:
  - headline: {type: string, description: the release headline}
  - published: {type: datetime, description: the release date}
  - url: {type: url, description: the link to the release}
optional: [url]
---
Every press release {company} published, newest first.
"""


def _run(coro: object) -> object:
    return asyncio.run(cast("asyncio.Future[object]", coro))


class _Llm:
    """A scripted model: one reply per call, recording every prompt; reports its own spend."""

    def __init__(self, replies: "list[str]") -> None:
        self.replies, self.prompts = replies, []  # type: ignore[var-annotated]
        self.spent_usd, self.calls = 0.0, 0

    async def complete(self, prompt: str) -> str:
        self.prompts.append(prompt)
        self.calls += 1
        self.spent_usd += 0.001
        return self.replies[min(len(self.prompts) - 1, len(self.replies) - 1)]


class _Search:
    def __init__(self, urls: "list[str]") -> None:
        self.urls, self.terms = urls, []  # type: ignore[var-annotated]

    async def __call__(self, goal: str) -> "list[str]":
        self.terms.append(goal)
        return self.urls


def test_brief_renders_its_arguments_everywhere_and_fails_loudly() -> None:
    brief = Brief.from_markdown(_BRIEF)
    assert brief.args == ["company"] and brief.placeholders() == {"company"}
    with pytest.raises(BriefError, match="needs company"):
        brief.render()
    with pytest.raises(BriefError, match="takes no argument ticker"):
        brief.render(company="intel", ticker="INTC")
    r = brief.render(company="intel")
    assert r.search.term == "intel investor relations press releases"
    assert r.search.domain[:2] == ["intel", "investors.intel"]
    assert r.goal == "Every press release intel published, newest first."
    assert r.required == ["headline", "published"] and r.names[-1] == "url"
    assert "LEAVE OUT" in r.scope() and "- published (datetime)" in r.schema_lines()
    assert r.expected_range() == (5, 100) and r.values == {"company": "intel"}


def test_state_round_trips_to_json_and_resumes_at_the_first_empty_slot(tmp_path: Path) -> None:
    state = Onboarding.start(Brief.from_markdown(_BRIEF), company="intel")
    assert state.next_stage() == "search"
    state.search = SearchResult(term="t", hits=[])
    state.charge("search", 0, 0.0)
    path = state.save(tmp_path / "s.json")
    back = Onboarding.load(path)
    assert back == state and back.next_stage() == "review_search"
    assert json.loads(path.read_text())["brief"]["values"] == {"company": "intel"}
    back.reset_from("search")
    assert back.search is None and back.next_stage() == "search"
    with pytest.raises(KeyError, match="unknown stage"):
        back.reset_from("nope")


def test_search_scores_hits_by_domain_and_path_hints() -> None:
    score, d, p = score_url(
        "https://investors.intel.com/news/press-releases",
        ["intel", "investors.intel"],
        ["news", "press"],
    )
    assert (score, d, p) == (6.0, ["intel", "investors.intel"], ["news", "press"])
    assert score_url("https://www.benzinga.com/quote/INTC/news", ["intel"], ["news"])[0] == 1.0


def test_search_then_review_search_stages_offline(tmp_path: Path) -> None:
    urls = [
        "https://www.benzinga.com/quote/INTC/news",  # third party: low score, model drops it
        "https://www.intel.com/content/www/us/en/newsroom/home.html",
        "https://www.intc.com/news-events/press-releases",  # hmm: no hint match (intc) -> score 0
        "https://investors.intel.com/news/press-releases",
    ]
    llm = _Llm(
        [
            json.dumps(
                {
                    "picks": [
                        {"n": 1, "tier": "must", "why": "the IR press-release listing"},
                        {"n": 2, "tier": "lead", "why": "the newsroom"},
                        {"n": 99, "tier": "must", "why": "out of range -- ignored"},
                    ]
                }
            )
        ]
    )
    search = _Search(urls)

    async def go() -> Onboarding:
        async with Resolver() as r:
            state = Onboarding.start(Brief.from_markdown(_BRIEF), company="intel")
            ctx = Context(resolver=r, llm=cast("object", llm), search=search)  # type: ignore[arg-type]
            return await run(state, ctx, until="review_search", save=tmp_path / "s.json")

    state = cast(Onboarding, _run(go()))
    assert search.terms == ["intel investor relations press releases"]
    assert state.search is not None and state.review_search is not None
    # scored best-first: the IR listing (domain x2 + path x2) leads; the third party trails
    assert state.search.hits[0].url == "https://investors.intel.com/news/press-releases"
    assert state.search.hits[0].score == 7.0 and state.search.hits[-1].score <= 1.0
    # the review prompt is TINY: goal + scope + one line per hit, no page content
    assert len(llm.prompts) == 1 and len(llm.prompts[0]) < 1200
    assert "1. [7] https://investors.intel.com/news/press-releases" in llm.prompts[0]
    picks = state.review_search.picks
    assert [(p.url, p.tier) for p in picks] == [
        ("https://investors.intel.com/news/press-releases", "must"),
        ("https://www.intel.com/content/www/us/en/newsroom/home.html", "lead"),
    ]
    assert len(state.review_search.dropped) == 2
    # spend attributed to the stage; the log has one line per stage; the file resumes
    assert state.spend.by_stage == {"review_search": pytest.approx(0.001)}
    assert [l.stage for l in state.log] == ["search", "review_search"]
    assert Onboarding.load(tmp_path / "s.json").next_stage() == "crawl"
