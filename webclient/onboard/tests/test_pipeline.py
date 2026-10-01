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


@pytest.fixture(autouse=True)
def _no_browser(monkeypatch: pytest.MonkeyPatch) -> None:
    """A test never launches a browser: the render seam serves the page at the HTTP tier."""
    import web.onboard.pipeline.stages.review_candidate as rc
    from web.fetch import Request

    async def http_render(ctx: object, url: str) -> object:
        return await cast("Context", ctx).resolver.snapshot(Request(url=url))

    monkeypatch.setattr(rc, "render", http_render)


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


def _site(httpserver: HTTPServer) -> None:
    """A tiny IR site: a home page (lead) linking to a press-release listing; a third-party page."""
    rows = "".join(
        f"<li class='release'><h3><a href='/news/release-{n}'>Release {n}</a></h3>"
        f"<time datetime='2026-09-{10 + n:02d}'>Sep {10 + n}</time><span class='tag'>Corporate</span></li>"
        for n in range(1, 9)
    )
    httpserver.expect_request("/").respond_with_data(
        "<html><body><nav><a href='/about'>About</a><a href='/news/'>News</a></nav>"
        "<main><h1>ACME investors</h1></main></body></html>",
        content_type="text/html",
    )
    httpserver.expect_request("/about").respond_with_data(
        "<html><body><main><p>About ACME</p></main></body></html>", content_type="text/html"
    )
    httpserver.expect_request("/news/").respond_with_data(
        f"<html><head><title>Press releases</title></head><body><main><ul class='list'>{rows}</ul>"
        "<a rel='next' href='/news/?page=2'>Next</a></main></body></html>",
        content_type="text/html",
    )
    for n in range(1, 9):
        httpserver.expect_request(f"/news/release-{n}").respond_with_data(
            f"<html><body><article><h1>Release {n}</h1><p>Body {n}</p></article></body></html>",
            content_type="text/html",
        )


def test_whole_pipeline_offline_from_a_lead_to_the_authored_query(
    httpserver: HTTPServer, tmp_path: Path
) -> None:
    # search -> review (a lead: the IR home) -> crawl reaches the listing and reviews it at once
    # (early stop) -> expand (records, pager, no api) -> location review -> resolve plan (page,
    # basic) -> extract: one shot misses `published` (wrong read), the repair fixes it -> review.
    _site(httpserver)
    home = httpserver.url_for("/")
    listing = httpserver.url_for("/news/")
    brief = Brief.from_markdown(
        _BRIEF.replace(
            'domain: ["{company}", "investors.{company}", "ir.{company}", "q4cdn", "gcs-web"]',
            'domain: ["localhost", "127.0.0.1"]',
        )
    )
    replies = [
        json.dumps({"picks": [{"n": 1, "tier": "lead", "why": "the IR home"}]}),  # review_search
        json.dumps(
            {"picks": [{"n": 1, "tier": "must", "why": "the news listing"}]}
        ),  # crawl round 1 (/about, /news/)
        json.dumps({"present": True, "reason": "a list of releases"}),  # review_candidate (/news/)
        json.dumps(
            {"ok": True, "summary": "the press-release listing", "concerns": []}
        ),  # review_location
        json.dumps(  # author_extract, one shot: published read as text (not a datetime)
            {
                "records": "li.release",
                "fields": {
                    "headline": {"css": "h3 a", "read": "text"},
                    "published": {"css": "time", "read": "attr:datetime"},
                    "url": {"css": "h3 a", "read": "href"},
                    "bogus": {"css": "x", "read": "text"},
                },
            }
        ),
        json.dumps({"ok": True, "notes": "eight releases, newest first"}),  # author_review
    ]
    llm = _Llm(replies)
    search = _Search([home, "https://www.benzinga.com/quote/ACME/news"])

    async def go() -> Onboarding:
        async with Resolver() as r:
            state = Onboarding.start(brief, company="acme")
            ctx = Context(resolver=r, llm=cast("object", llm), search=search)  # type: ignore[arg-type]
            await run(state, ctx, until="crawl", save=tmp_path / "s.json")
            # RESUME from the saved file with a fresh context (the page is fetched again)
            resumed = Onboarding.load(tmp_path / "s.json")
            assert resumed.next_stage() == "review_candidate"
            ctx2 = Context(resolver=r, llm=cast("object", llm), search=search)  # type: ignore[arg-type]
            return await run(resumed, ctx2, save=tmp_path / "s.json")

    state = cast(Onboarding, _run(go()))
    assert state.stopped == "", state.stopped
    assert state.crawl is not None and state.crawl.stopped_early
    assert [v.url for v in state.crawl.visited][:1] == [home]
    assert state.crawl.candidates[0].url == listing
    assert state.review_candidate is not None and state.review_candidate.url == listing
    assert state.review_candidate.present and not state.review_candidate.retried_browser
    src = state.expand
    assert src is not None and "record_list" in src.flags  # a signal, never a selector
    assert src.pagination is not None and src.pagination.next_selector == "a[rel=next]"
    assert src.api is None and src.spa is None
    assert state.review_location is not None and state.review_location.ok
    assert state.author_resolve is not None and state.author_resolve.url == listing
    assert state.author_resolve.profile == "basic" and not state.author_resolve.via_api
    ex = (state.author_extract or [None])[0]
    assert ex is not None and ex.complete and ex.row_count == 8, ex.attempts
    assert set(ex.fields) == {"headline", "published", "url"}  # the bogus field was dropped
    assert ex.record_selector == "li.release" and "select_all('li.release')" in ex.source
    assert "time" in ex.fields["published"]
    first = cast("dict[str, object]", ex.sample[0])
    assert first["headline"] == "Release 1" and first["published"] == "2026-09-11"
    assert str(first["url"]).endswith("/news/release-1")
    assert state.author_review and state.author_review[0].ok
    # cost shape: six small calls, every prompt under the budget, spend attributed per stage
    assert len(llm.prompts) == 6 and all(len(p) < 4500 for p in llm.prompts), [
        len(p) for p in llm.prompts
    ]
    assert set(state.spend.by_stage) == {
        "review_search",
        "crawl",
        "review_candidate",
        "review_location",
        "author_extract",
        "author_review",
    }
    assert [l.stage for l in state.log] == list(
        __import__("web.onboard.pipeline", fromlist=["STAGE_NAMES"]).STAGE_NAMES
    )
    # the state file is the whole onboarding: it reloads equal to the in-memory state
    assert Onboarding.load(tmp_path / "s.json") == state


def test_review_candidate_retries_through_a_browser_once_and_rejects(
    httpserver: HTTPServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    import web.onboard.pipeline.stages.review_candidate as rc
    from web.fetch import Request, Snapshot
    from web.onboard.pipeline.stages.review_candidate import review_one

    rendered: list[str] = []

    async def fake_render(ctx: object, url: str) -> Snapshot:  # the "browser" shows the same shell
        rendered.append(url)
        return Snapshot(
            request=Request(url=url),
            status=200,
            content=b"<html><body><div id='app'></div></body></html>",
            headers={"content-type": "text/html"},
        )

    monkeypatch.setattr(rc, "render", fake_render)
    httpserver.expect_request("/shell").respond_with_data(
        "<html><body><div id='app'></div><script>window.__x=1</script></body></html>",
        content_type="text/html",
    )
    llm = _Llm([json.dumps({"present": False, "reason": "an empty shell"})])

    async def go() -> object:
        async with Resolver() as r:
            state = Onboarding.start(Brief.from_markdown(_BRIEF), company="acme")
            ctx = Context(resolver=r, llm=cast("object", llm), search=_Search([]))  # type: ignore[arg-type]
            return await review_one(state, ctx, httpserver.url_for("/shell"))

    review = cast("rc.CandidateReview", _run(go()))
    assert not review.present and review.retried_browser and review.profile == "full_browser"
    assert rendered == [httpserver.url_for("/shell")] and len(llm.prompts) == 2


def test_expand_reads_a_page_parameter_pager_and_api_knobs(httpserver: HTTPServer) -> None:
    from web.onboard.pipeline.stages.expand import knobs_of, pager_of
    from web.parse import parse

    doc = parse(
        b"<html><body><ul>"
        + b"".join(b"<li class=r><a href='/x'>a</a></li>" for _ in range(4))
        + b"</ul><a href='/news/?page=2'>2</a><a href='/news/?page=3'>3</a></body></html>",
        url="http://x/news/",
        content_type="text/html",
    )
    pager = pager_of(doc, "next_link", "a pager")
    assert pager.kind == "param" and pager.param == "page"
    nxt = parse(
        b"<a rel=next href='/news/?p=2'>n</a>", url="http://x/news/", content_type="text/html"
    )
    assert pager_of(nxt, "next_link", "").next_selector == "a[rel=next]"
    assert pager_of(doc, "scroll", "").kind == "scroll"
    assert knobs_of("https://x/feed/Event.svc/GetEventList?year=2026&pageSize=-1&type=") == {
        "year": "2026",
        "pageSize": "-1",
    }


def test_author_review_marks_the_nested_seam_and_spend_estimates_the_api_cost() -> None:
    from web.onboard.pipeline import AuthorReview, ExtractQuery
    from web.onboard.pipeline.stages import author_review

    brief = Brief.from_markdown(
        _BRIEF.replace("optional: [url]", "optional: []").replace(
            "  - url: {type: url, description: the link to the release}",
            "  - url: {type: url, description: the link to the release}\n  - body: {type: string, description: the full text, on the release page}",
        )
    ).render(company="acme")
    state = Onboarding(brief=brief)
    state.author_extract = [
        ExtractQuery(
            fields={
                "headline": "wq.doc.select('h3').attr('text')",
                "url": "wq.doc.select('a').attr('href')",
                "published": "x",
            },
            row_count=3,
            sample=[
                {"headline": "h", "url": "http://x/1", "published": "2026-01-01", "_identity": "i"}
            ],
            complete=True,
        )
    ]
    llm = _Llm([json.dumps({"ok": True, "notes": "fine"})])

    async def go() -> "list[AuthorReview]":
        ctx = Context(resolver=cast("object", None), llm=cast("object", llm), search=_Search([]))  # type: ignore[arg-type]
        return await author_review.run(state, ctx)

    review = cast("list[AuthorReview]", _run(go()))[0]
    assert review.ok and review.next == "nested"
    assert review.detail_field == "url" and review.pending == ["body"]
    assert "_identity" not in llm.prompts[0] and "Optional fields" in llm.prompts[0]
    # the spend carries the prompt / reply sizes; the API estimate prices them at Haiku rates
    assert state.spend.chars_in > 100 and state.spend.chars_out > 10
    assert 0 < state.spend.api_estimate() < 0.001


def test_expand_renders_once_when_the_http_page_is_thin_and_switches_to_the_browser(
    httpserver: HTTPServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    # the lesson kept from the old locate: a script app's HTTP shell shows two rows and passes a
    # skeleton read; the dataset appears only rendered. Expand renders ONCE, compares, and bakes
    # the browser tier when the render shows materially more content.
    import web.onboard.pipeline.stages.review_candidate as rc
    from web.fetch import Request, Snapshot
    from web.onboard.pipeline import CandidateReview
    from web.onboard.pipeline.stages import expand

    shell = (
        "<html><body><ul>"
        + "".join(f"<li class=ev><a href='/e{n}'>Event {n}</a></li>" for n in range(2))
        + "</ul><script>app()</script></body></html>"
    )
    httpserver.expect_request("/events").respond_with_data(shell, content_type="text/html")
    full = (
        "<html><body><ul>"
        + "".join(
            f"<li class=ev><a href='/e{n}'>Event {n}</a><time datetime='2026-10-{n + 1:02d}'>d</time>"
            f"<p>Event {n} is a conference presentation with a webcast replay and slides.</p></li>"
            for n in range(14)
        )
        + "</ul></body></html>"
    ).encode()

    async def fake_render(ctx: object, url: str) -> Snapshot:
        return Snapshot(
            request=Request(url=url),
            status=200,
            content=full,
            headers={"content-type": "text/html"},
        )

    monkeypatch.setattr(rc, "render", fake_render)
    state = Onboarding.start(Brief.from_markdown(_BRIEF), company="acme")
    state.review_candidate = CandidateReview(
        url=httpserver.url_for("/events"), present=True, profile="basic"
    )

    async def go() -> object:
        async with Resolver() as r:
            ctx = Context(resolver=r, llm=cast("object", None), search=_Search([]))  # type: ignore[arg-type]
            return await expand.run(state, ctx)

    src = cast("expand.DatasetSource", _run(go()))
    assert src.profile == "full_browser"
    assert (
        src.spa is not None
        and "reviewed through a browser" in src.spa.reason
        or src.spa is not None
    )


def test_optional_rewrite_and_presence_go_through_the_plan() -> None:
    from web.onboard.pipeline.stages.author_extract import _optional, _presence

    chain = "wq.doc.select('td:nth-child(2) a, td:nth-child(2)').attr('text')"
    soft = "wq.doc.select('td:nth-child(2) a, td:nth-child(2)', optional=True).attr('text')"
    assert _optional(chain) == soft
    assert _presence(chain) == "~" + soft + ".is_empty()"  # the VALUE, not just the element
    already = "wq.doc.select('.x', optional=True).attr('text')"
    assert _optional(already) == already
    assert _presence("wq.doc.attr('k')") == "~wq.doc.attr('k').is_empty()"


def test_a_rejected_review_sends_the_extraction_back_once_with_its_note(
    httpserver: HTTPServer, tmp_path: Path
) -> None:
    _site(httpserver)
    brief = Brief.from_markdown(
        _BRIEF.replace(
            'domain: ["{company}", "investors.{company}", "ir.{company}", "q4cdn", "gcs-web"]',
            'domain: ["localhost", "127.0.0.1"]',
        )
    )
    good = {
        "headline": {"css": "h3 a", "read": "text"},
        "published": {"css": "time", "read": "attr:datetime"},
        "url": {"css": "h3 a", "read": "href"},
    }
    bad = dict(good, published={"css": "span.tag", "read": "text"})  # reads the tag as the date
    good_reply = {"records": "li.release", "fields": good}
    bad_reply = {"records": "li.release", "fields": bad}
    replies = [
        json.dumps({"picks": [{"n": 1, "tier": "must", "why": "the listing"}]}),
        json.dumps({"present": True, "reason": "a list of releases"}),
        json.dumps({"ok": True, "summary": "the listing", "concerns": []}),
        json.dumps(bad_reply),  # extract 1
        json.dumps(
            {"ok": False, "notes": "published holds the category tag, not the date"}
        ),  # review 1
        json.dumps(good_reply),  # extract 2 (the repair pass)
        json.dumps({"ok": True, "notes": "dates are right now"}),  # review 2
    ]
    llm = _Llm(replies)

    async def go() -> Onboarding:
        async with Resolver() as r:
            state = Onboarding.start(brief, company="acme")
            ctx = Context(resolver=r, llm=cast("object", llm), search=_Search([httpserver.url_for("/news/")]))  # type: ignore[arg-type]
            return await run(state, ctx, save=tmp_path / "s.json")

    state = cast(Onboarding, _run(go()))
    assert state.repairs == 1 and state.author_review and state.author_review[0].ok
    ex = (state.author_extract or [None])[0]
    assert ex is not None and "time" in ex.fields["published"] and ex.row_count == 8
    assert "REPAIR -- a reviewer rejected" in llm.prompts[5] and "category tag" in llm.prompts[5]
    assert [l.stage for l in state.log][-4:] == [
        "author_extract",
        "author_review",
        "author_extract",
        "author_review",
    ]


def test_the_skeleton_is_always_the_full_outline_without_detector_marks() -> None:
    # USER: "the skeleton should always be the full outline" (a small outline made authoring
    # fail; a centre-clipped one hid the records from the review). Full depth and width, chrome
    # dropped, no `select_all(...)` mark (an upstream pointer), only a safety cap.
    from web.onboard.pipeline.stages.review_candidate import SKELETON_CHARS, skeleton
    from web.parse import parse

    deep = "<div>" * 20 + "<span class=deep-leaf>leaf</span>" + "</div>" * 20
    rows = "".join(f"<li class=ev><a href='/e{i}'>Event {i}</a>{deep}</li>" for i in range(40))
    html = f"<html><body><nav><a href='/'>home</a></nav><main><ul class=events>{rows}</ul></main></body></html>"
    doc = parse(html.encode(), url="http://x/", content_type="text/html")
    out = skeleton(doc)
    assert "deep-leaf" in out and "Event 39" in out and len(out) <= SKELETON_CHARS + 100
    assert "select_all(" not in out and "RECORD LIST" not in out  # no detector pointer
    assert "nav" not in out.split("main")[0]  # chrome dropped


def test_a_brief_with_several_guides_authors_one_query_each_and_run_joins_them(
    httpserver: HTTPServer,
) -> None:
    # USER: "split the events brief into two queries -- a brief contains several authoring guides
    # -> several authoring steps". Each guide is one extract + one review; run() joins the rows.
    from web.onboard import run as run_queries

    up = "".join(
        f"<li class=up><b>Up {i}</b><time datetime='2027-01-0{i}'>d</time></li>"
        for i in range(1, 3)
    )
    past = "".join(
        f"<li class=past><b>Past {i}</b><time datetime='2025-01-0{i}'>d</time></li>"
        for i in range(1, 5)
    )
    httpserver.expect_request("/events").respond_with_data(
        f"<html><body><main><h2>Upcoming</h2><ul>{up}</ul><h2>Past</h2><ul>{past}</ul></main></body></html>",
        content_type="text/html",
    )
    brief = Brief.from_markdown("""---
name: ev
args: [company]
search: {term: "{company} events", domain: ["localhost", "127.0.0.1"]}
queries:
  - {name: upcoming, hint: "only the upcoming events"}
  - {name: past, hint: "only the past events", fields: [title]}
schema:
  - title: {type: string, description: the event}
  - when: {type: datetime, description: the date}
---
events of {company}
""")
    assert [g.name for g in brief.guides()] == ["upcoming", "past"]
    replies = [
        json.dumps({"picks": [{"n": 1, "tier": "must", "why": "the events page"}]}),
        json.dumps({"present": True, "reason": "two event lists"}),
        json.dumps({"ok": True, "summary": "the events page", "concerns": []}),
        json.dumps(
            {
                "records": "li.up",
                "fields": {
                    "title": {"css": "b", "read": "text"},
                    "when": {"css": "time", "read": "attr:datetime"},
                },
            }
        ),
        json.dumps({"records": "li.past", "fields": {"title": {"css": "b", "read": "text"}}}),
        json.dumps({"ok": True, "notes": "two upcoming"}),
        json.dumps({"ok": True, "notes": "four past"}),
    ]
    llm = _Llm(replies)

    async def go() -> "tuple[Onboarding, object]":
        async with Resolver() as r:
            state = Onboarding.start(brief, company="acme")
            ctx = Context(resolver=r, llm=cast("object", llm), search=_Search([httpserver.url_for("/events")]))  # type: ignore[arg-type]
            await run(state, ctx)
            return state, await run_queries(state, resolver=r)

    state, result = cast("tuple[Onboarding, object]", _run(go()))
    assert state.stopped == "", state.stopped
    assert [ex.name for ex in state.author_extract or []] == ["upcoming", "past"]
    assert [ex.row_count for ex in state.author_extract or []] == [2, 4]
    assert (
        "THIS QUERY: past" in llm.prompts[4]
        and "when" not in llm.prompts[4].split("FIELDS:")[1].split("This is")[0]
    )
    assert [r.name for r in state.author_review or []] == ["upcoming", "past"] and all(
        r.ok for r in state.author_review or []
    )
    rows = cast("list[dict[str, object]]", result.rows)  # type: ignore[attr-defined]
    assert [r["title"] for r in rows] == ["Up 1", "Up 2", "Past 1", "Past 2", "Past 3", "Past 4"]
    assert result.report.rows == 6  # type: ignore[attr-defined]


def test_an_invalid_css_selector_from_the_model_is_a_repair_not_a_crash(
    httpserver: HTTPServer,
) -> None:
    # USER: "a broken css selector in author extract crashed the whole pipeline instead of
    # handling it and retrying the LLM"
    from web.onboard.pipeline.stages.author_extract import matches
    from web.parse import parse

    doc = parse(b"<ul><li class=r><b>A</b></li></ul>", url="http://x/", content_type="text/html")
    assert matches(doc, "li.r") == (1, "")
    n, why = matches(doc, "li.r[")  # unterminated attribute selector
    assert n == 0 and "not a valid selector" in why
    _site(httpserver)
    brief = Brief.from_markdown(
        _BRIEF.replace(
            'domain: ["{company}", "investors.{company}", "ir.{company}", "q4cdn", "gcs-web"]',
            'domain: ["localhost", "127.0.0.1"]',
        )
    )
    replies = [
        json.dumps({"picks": [{"n": 1, "tier": "must", "why": "the listing"}]}),
        json.dumps({"present": True, "reason": "a list"}),
        json.dumps({"ok": True, "summary": "the listing", "concerns": []}),
        json.dumps(
            {"records": "li.release[", "fields": {"headline": {"css": "h3 a", "read": "text"}}}
        ),  # broken
        json.dumps(
            {
                "records": "li.release",
                "fields": {
                    "headline": {"css": "h3 a(", "read": "text"},
                    "published": {"css": "time", "read": "attr:datetime"},
                },
            }
        ),  # a broken FIELD
        json.dumps(
            {
                "records": "li.release",
                "fields": {
                    "headline": {"css": "h3 a", "read": "text"},
                    "published": {"css": "time", "read": "attr:datetime"},
                },
            }
        ),
        json.dumps({"ok": True, "notes": "fine"}),
    ]
    llm = _Llm(replies)

    async def go() -> Onboarding:
        async with Resolver() as r:
            state = Onboarding.start(brief, company="acme")
            ctx = Context(resolver=r, llm=cast("object", llm), search=_Search([httpserver.url_for("/news/")]))  # type: ignore[arg-type]
            return await run(state, ctx)

    state = cast(Onboarding, _run(go()))
    ex = (state.author_extract or [None])[0]
    assert ex is not None and ex.complete and ex.row_count == 8, state.stopped
    assert "not a valid selector" in ex.attempts[0] and "not a valid selector" in llm.prompts[4]
    assert len(ex.attempts) == 3


def test_the_author_may_narrow_a_query_with_a_where_predicate() -> None:
    # 10x: both guides returned the same 74 feed rows -- a date predicate splits upcoming / past
    from web.onboard.pipeline.stages.author_extract import _check_where, compile_source

    src = compile_source(
        "GetEventListResult",
        {"t": "wq.doc.attr('Title')"},
        where="wq.doc.attr('StartDate').datetime() >= '2026-10-01'",
    )
    assert (
        src
        == "wq.doc.select_all('GetEventListResult').filter(wq.doc.attr('StartDate').datetime() >= '2026-10-01').extract(t=wq.doc.attr('Title'))"
    )
    assert _check_where("") == "" and _check_where("wq.doc.attr('d') < '2026-10-01'") == ""
    assert "not a valid" in _check_where("wq.doc.attr('d') >=")
    assert "not a valid" in _check_where("1 == 1")  # a bool, not a wq chain


def test_expand_describes_year_tabs_and_the_author_hears_the_latest_data_rule() -> None:
    # USER (the Adobe case): data before 2026 sits in a different container with a different
    # format under year tabs; nothing told the author. Expand DESCRIBES the tabs (which year is
    # selected); the author prompt carries the source description and the latest-data rule.
    from web.onboard.pipeline import DatasetSource
    from web.onboard.pipeline.ask import render
    from web.onboard.pipeline.stages.expand import filters_of
    from web.onboard.pipeline.stages.review_location import describe
    from web.parse import parse

    html = (
        "<html><body><ul class=tabs><li><a class=active href='#2026'>2026</a></li><li><a href='#2025'>2025</a></li>"
        "<li><a href='#2024'>2024</a></li></ul><select><option selected>2026</option><option>2025</option></select>"
        "<div id=y2026><article class=r>A</article></div><div id=y2025><table><tr><td>B</td></tr></table></div></body></html>"
    )
    doc = parse(html.encode(), url="http://x/", content_type="text/html")
    found = filters_of(doc)
    assert any("year selector: 2026 (selected), 2025" in f for f in found)
    assert any("year tabs / links: 2026 (selected), 2025, 2024" in f for f in found)
    src = DatasetSource(url="http://x/", filters=found, filtered=True)
    assert "filters: year tabs" in describe(src)
    prompt = render(
        "author_extract",
        goal="g",
        schema="- a",
        kind="an HTML document",
        skeleton="<ul>",
        hint="",
        source=describe(src),
        today="2026-10-01",
        note="",
    )
    assert prompt.startswith("NOW: ") and "year tabs" in prompt and "LATEST data" in prompt
