"""onboarding.orchestrate -- see the package docstring."""


import abc
import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Callable, Literal, Sequence

from pydantic import BaseModel, model_validator

#: the pipeline's logger. Stages log progress here (seeds, crawl, candidates, the
#: evaluation, the query, spend); a CLI or app sets the level / handler. Each line is
#: also appended to ``OnboardingResult.steps`` for a programmatic trace.

from ...core.crawl import from_picks
from ...core.document.models import Flag
from ...policy import (
    AntiBotPolicy,
    BrowserPolicy,
    ProxyPolicy,
    Resolve,
)
from ...llm.guides import lazy_query_guide
from ...query.expr import from_blob
from ...interface import Reference, WebClient, wq
from ...clients.llm import Budget, BudgetExceeded, LlmClient, LlmError
from ...llm.prompts import render_prompt

from .common import LLM, SearchFn, _mode
from .artifacts import Brief, OnboardingResult, _RunArtifacts, _ensure_logging, _trace, _summarize
from .llm import _read_flags
from .search import search_web
from .crawl import crawl_from_seeds
from .select import select_candidates
from .evaluate import evaluate_candidates
from .reference import _source_url, write_reference, write_resolve
from .query import _recency_guidance, write_query
from .review import review_crawl, review_select, review_query, review_query_exit, _note_review, diagnose_failure


# --------------------------------------------------------------------------- #
# The orchestrator.
# --------------------------------------------------------------------------- #


def onboard_company(
    company: str,
    brief: Brief,
    *,
    wc: WebClient,
    llm: LLM,
    search: SearchFn,
    max_pages: int = 20,
    browser: bool = True,
    budget: Budget | None = None,
    review: bool = False,
    author_engine: str = "text",
) -> OnboardingResult:
    """Run the whole pipeline for one company: search -> crawl -> select -> evaluate
    -> write the reference, resolve, and query for the best source found.

    Pass a :class:`~webclient.clients.llm.Budget` to cap LLM spend for this run: when
    an :class:`~webclient.clients.llm.LlmClient` is the injected ``llm`` the budget is
    attached to it, and if the cap is hit mid-pipeline the run stops and reports
    ``ok=False`` / ``reason="llm budget exceeded"`` instead of raising to the caller.

    ``review=True`` runs the meta-review stage after the pipeline: the model grades the
    run's choices (the crawl, the URL selection, the authored query -- and, on a failure,
    diagnoses what went wrong). The reviews land on ``result.reviews`` and in the summary.
    Off by default (it costs extra LLM calls)."""
    _ensure_logging()  # progress is always visible
    result = OnboardingResult(company=company, brief=brief)
    artifacts = _RunArtifacts()
    # Thread the cap into an LlmClient so its per-call spend is enforced. A plain
    # callable llm (e.g. a test stub) carries no cost, so there is nothing to cap.
    if budget is not None and isinstance(llm, LlmClient):
        llm.budget = budget
    try:
        _onboard_company(
            company, brief, result, artifacts,
            wc=wc, llm=llm, search=search, max_pages=max_pages, browser=browser,
            review=review,  # stage reviews are integral + gating, run inline (see _onboard_company)
            author_engine=author_engine,
        )
        if review and not result.ok:  # diagnose WHY it failed (a gate, or a stage error)
            diagnose_failure(result, artifacts, brief=brief, llm=llm)
    except BudgetExceeded:
        result.ok = False
        result.reason = result.reason or "llm budget exceeded"
    if isinstance(llm, LlmClient):
        result.cost_usd = llm.spent_usd
    _summarize(result)  # always print the end-of-run summary
    return result


def _onboard_company(
    company: str,
    brief: Brief,
    result: OnboardingResult,
    artifacts: _RunArtifacts,
    *,
    wc: WebClient,
    llm: LLM,
    search: SearchFn,
    max_pages: int,
    browser: bool,
    review: bool = False,
    author_engine: str = "text",
) -> OnboardingResult:
    def note(msg: str, *a: Any) -> None:  # trace with the running spend kept current
        if isinstance(llm, LlmClient):
            result.cost_usd = llm.spent_usd
        _trace(result, msg, *a)

    note("searching the web for seeds")
    seeds = search_web(brief, company, search=search, llm=llm)
    artifacts.seeds = list(seeds)
    if not seeds:
        result.reason = "no search seeds"
        return result
    note("%d seed(s); crawling for the dataset", len(seeds))
    crawl = crawl_from_seeds(
        seeds, brief, wc=wc, llm=llm, company=company, max_pages=max_pages, browser=browser
    )
    artifacts.crawl = crawl
    note("crawled %d page(s), %d failed", len(crawl.pages), len(crawl.failures))
    # the crawl review is a FLAG for the human, not a gate -- record it and press on.
    if review:
        _note_review(result, review_crawl(artifacts, brief, llm=llm))
    candidates = select_candidates(
        crawl, brief, llm=llm, seed_urls=[s.url for s in artifacts.seeds if s.url]
    )
    artifacts.candidates = list(candidates)
    if not candidates:
        result.reason = "no candidate pages"
        return result
    note("%d candidate(s); evaluating best-first", len(candidates))
    evaluation = evaluate_candidates(
        candidates, brief, wc=wc, llm=llm, browser=_mode(browser)
    )
    if evaluation is None or not evaluation.dataset_present:
        result.reason = "no usable source found"
        result.evaluation = evaluation
        return result
    result.evaluation = evaluation
    # a brief-level EXIT CONDITION held on the chosen source -> stop CLEANLY (a defined exit,
    # not a failure): e.g. ir-events with no upcoming events, whose structure we can't know.
    if evaluation.exit_when_met:
        result.exited = True
        result.reason = f"exit condition met: {evaluation.exit_reason or brief.exit_when}"
        note("brief exit condition met — %s", evaluation.exit_reason or brief.exit_when)
        if review:
            _note_review(result, review_select(result, artifacts, brief, llm=llm))
        return result
    note(
        "chose %s (queryable=%s, scrapability=%d)",
        evaluation.url, evaluation.is_queryable, evaluation.scrapability,
    )
    # the selection review is a FLAG for the human, not a gate -- record it and press on.
    if review:
        _note_review(result, review_select(result, artifacts, brief, llm=llm))
    # -- the flag-driven decision cascade for the chosen source, in order ----------
    # (1) reference + query URL: the same source -- a same-origin data API only when the page is
    # an SPA shell backed by it, otherwise the page itself (see _source_url).
    result.reference = write_reference(evaluation, wc=wc)
    query_url = _source_url(evaluation)
    doc = wc.fetch(query_url, browser=_mode(browser), optional=True)
    artifacts.query_doc = doc
    flags = _read_flags(doc) if doc.ok else {}
    # (2) a login wall on the source itself -> no query reaches the data; stop.
    if flags.get("login_required") is not None and flags["login_required"].present:
        result.reason = "the source requires login"
        return result
    # (3) resolve policy: spa -> browser, anti_bot_triggered -> proxy/stealth.
    result.resolve = write_resolve(list(flags.values()))
    fired = [n for n, f in flags.items() if f.present]
    note("flags fired: %s; authoring the query", ", ".join(fired) or "none")
    # (4) query: authored from the skeleton, told to page when the source paginates. The source
    # was already fetched above (for the flags) -- reuse that Document so write_query doesn't
    # re-fetch (a browser/proxy re-fetch is real budget + latency, and can drift the skeleton).
    result.query = write_query(
        query_url, brief, wc=wc, llm=llm, browser=_mode(browser),
        paginated=evaluation.has_pagination, resolve=result.resolve,
        doc=doc if doc.ok else None,
        recency=_recency_guidance(evaluation),  # sort order + where the most recent records are
        author_engine=author_engine,  # "text" (write query code) | "index" (pick indexes -> build_query)
    )
    if isinstance(llm, LlmClient):
        result.cost_usd = llm.spent_usd
    # a real success EXTRACTS data with every required field: a query that ran but produced
    # 0 rows, or left a required field empty on every row, is NOT ok (a fallback `best`
    # artifact is kept for the summary but is marked not-complete).
    q = result.query
    result.ok = q is not None and q.complete and q.row_count > 0
    if result.ok:
        result.reason = ""
    elif q is not None and q.row_count > 0:
        result.reason = "authored query is missing required field(s)"
    elif q is not None:
        result.reason = "authored query extracted 0 rows"
    else:
        result.reason = "could not author a query"
    if q is not None:
        note("query authored (tested=%s, %d row[s])", q.tested, q.row_count)
    # the query review + the deterministic timeliness assessment are FLAGS for the human
    # (recorded on result.reviews), not gates: a working query is never discarded because a
    # model graded it low or the data looks stale.
    if review and q is not None:
        _note_review(result, review_query(result, artifacts, brief, llm=llm))
        # the brief's own EXIT CONDITION, re-checked against the query RESULT (not just the page):
        # ensures e.g. ir-events' "upcoming is empty" is NOTED against the data, and a probable
        # miss (an oddly-formatted row skipped) is flagged. A flag, never a gate.
        if brief.exit_when:
            _note_review(result, review_query_exit(result, artifacts, brief, llm=llm))
    return result


def onboard(
    companies: Sequence[str],
    brief: Brief,
    *,
    wc: WebClient,
    llm: LLM,
    search: SearchFn,
    max_pages: int = 20,
    browser: bool = True,
    budget: Budget | None = None,
    review: bool = False,
) -> list[OnboardingResult]:
    """Onboard several companies for the same brief (sequentially, one crawl each).

    A shared ``budget`` caps LLM spend across the WHOLE run: once it is exhausted the
    remaining companies report ``ok=False`` / ``reason="llm budget exceeded"``.
    ``review=True`` runs the meta-review stage for each company (see :func:`onboard_company`)."""
    return [
        onboard_company(
            c, brief, wc=wc, llm=llm, search=search, max_pages=max_pages,
            browser=browser, budget=budget, review=review,
        )
        for c in companies
    ]
