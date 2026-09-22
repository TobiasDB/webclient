"""onboarding.orchestrate -- the pipeline as a :class:`~webclient.pipeline.Pipeline`.

The stages (search -> crawl -> select -> evaluate -> [confirm] -> source -> query) are the
existing stage functions wrapped as :class:`~webclient.pipeline.Stage` s over one run
context; the gates carry the exact stop reasons the summary reports; the reviews are
FLAGS (recorded, never gating), as the ethos review decided. Every stage boundary is a
``PipelineEvent`` on the client's bus (so a trace / UI draws the run), and with
``interactive=True`` the run CHECKPOINTS after the evaluation -- the result comes back
with ``pending`` (an :class:`~webclient.loop.Ask`: "proceed with this source?") and
``result.resume("yes")`` continues -- the human-in-the-loop review gate.
"""


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

from ...loop import Ask
from ...pipeline import Pipeline, PipelineRun, Stage
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
    interactive: bool = False,
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
    Off by default (it costs extra LLM calls).

    ``interactive=True`` pauses after the evaluation with ``result.pending`` (an ``Ask``
    naming the chosen source); ``result.resume("yes")`` continues, ``"no"`` stops cleanly."""
    _ensure_logging()  # progress is always visible
    result = OnboardingResult(company=company, brief=brief)
    artifacts = _RunArtifacts()
    # Thread the cap into an LlmClient so its per-call spend is enforced. A plain
    # callable llm (e.g. a test stub) carries no cost, so there is nothing to cap.
    if budget is not None and isinstance(llm, LlmClient):
        llm.budget = budget
    pipeline = _build_pipeline(
        company, brief, result, artifacts, wc=wc, llm=llm, search=search, max_pages=max_pages,
        browser=browser, review=review, author_engine=author_engine, interactive=interactive,
    )

    def finish(run: PipelineRun) -> OnboardingResult:
        if run.waiting:
            result.pending = run.ask
            return result
        result.pending = None
        if review and not result.ok and not result.exited:  # diagnose WHY it failed
            diagnose_failure(result, artifacts, brief=brief, llm=llm)
        if isinstance(llm, LlmClient):
            result.cost_usd = llm.spent_usd
        _summarize(result)  # always print the end-of-run summary
        return result

    def guarded(fn: Any) -> OnboardingResult:
        try:
            return finish(fn())
        except BudgetExceeded:
            result.ok = False
            result.reason = result.reason or "llm budget exceeded"
            result.pending = None
            if isinstance(llm, LlmClient):
                result.cost_usd = llm.spent_usd
            _summarize(result)
            return result

    result._resume = lambda answer: guarded(lambda: pipeline.resume(answer))
    return guarded(lambda: pipeline.run(_Ctx(result=result, artifacts=artifacts)))


@dataclass
class _Ctx:
    """The pipeline's run context: the result being filled in, the live artifacts, and the
    answer to a checkpoint (set by ``resume``). Stage outputs land here by stage name too."""

    result: OnboardingResult
    artifacts: _RunArtifacts
    answer: Any = None
    search: Any = None
    crawl: Any = None
    select: Any = None
    evaluate: Any = None
    confirm: Any = None
    source: Any = None
    query: Any = None


def _build_pipeline(
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
    review: bool,
    author_engine: str,
    interactive: bool,
) -> Pipeline:
    """The onboarding pipeline for one company as stages over a :class:`_Ctx`."""

    def note(msg: str, *a: Any) -> None:  # trace with the running spend kept current
        if isinstance(llm, LlmClient):
            result.cost_usd = llm.spent_usd
        _trace(result, msg, *a)

    def stop(reason: str) -> str:  # a gate's stop: the summary's reason
        result.reason = reason
        return reason

    # -- 1. search -------------------------------------------------------------
    def run_search(ctx: _Ctx) -> Any:
        note("searching the web for seeds")
        seeds = search_web(brief, company, search=search, llm=llm)
        artifacts.seeds = list(seeds)
        return seeds

    # -- 2. crawl (LLM-driven frontier; the review is a flag) ---------------------
    def run_crawl(ctx: _Ctx) -> Any:
        note("%d seed(s); crawling for the dataset", len(ctx.search))
        crawl = crawl_from_seeds(
            ctx.search, brief, wc=wc, llm=llm, company=company, max_pages=max_pages, browser=browser
        )
        artifacts.crawl = crawl
        note("crawled %d page(s), %d failed", len(crawl.pages), len(crawl.failures))
        return crawl

    def crawl_review(ctx: _Ctx, crawl: Any) -> Any:
        if not review:
            return None
        r = review_crawl(artifacts, brief, llm=llm)
        _note_review(result, r)
        return r

    # -- 3. select -------------------------------------------------------------
    def run_select(ctx: _Ctx) -> Any:
        candidates = select_candidates(
            ctx.crawl, brief, llm=llm, seed_urls=[s.url for s in artifacts.seeds if s.url]
        )
        artifacts.candidates = list(candidates)
        return candidates

    # -- 4. evaluate (the brief's exit condition is a CLEAN stop, not a failure) -------
    def run_evaluate(ctx: _Ctx) -> Any:
        note("%d candidate(s); evaluating best-first", len(ctx.select))
        evaluation = evaluate_candidates(ctx.select, brief, wc=wc, llm=llm, browser=_mode(browser))
        result.evaluation = evaluation
        return evaluation

    def evaluate_gate(ctx: _Ctx, evaluation: Any) -> "bool | str":
        if evaluation is None or not evaluation.dataset_present:
            return stop("no usable source found")
        if evaluation.exit_when_met:
            result.exited = True
            reason = f"exit condition met: {evaluation.exit_reason or brief.exit_when}"
            note("brief exit condition met — %s", evaluation.exit_reason or brief.exit_when)
            if review:
                _note_review(result, review_select(result, artifacts, brief, llm=llm))
            return stop(reason)
        note(
            "chose %s (queryable=%s, scrapability=%d)",
            evaluation.url, evaluation.is_queryable, evaluation.scrapability,
        )
        return True

    def select_review(ctx: _Ctx, evaluation: Any) -> Any:
        # the selection review is a FLAG for the human, not a gate (only once a source is chosen)
        if not review or evaluation is None or not evaluation.dataset_present or evaluation.exit_when_met:
            return None
        r = review_select(result, artifacts, brief, llm=llm)
        _note_review(result, r)
        return r

    # -- 5. confirm (interactive only): the human-in-the-loop review gate -------------
    def run_confirm(ctx: _Ctx) -> Any:
        if ctx.answer is not None:
            return ctx.answer
        ev = ctx.evaluate
        return Ask(
            reason=f"proceed with {ev.url}?", options=["yes", "no"],
            detail={"url": ev.url, "queryable": ev.is_queryable, "scrapability": ev.scrapability,
                    "completeness": ev.completeness, "paginated": ev.has_pagination},
        )

    def confirm_gate(ctx: _Ctx, answer: Any) -> "bool | str":
        if str(answer).strip().lower() in ("no", "n", "false", "0", "stop"):
            result.exited = True
            return stop("declined at the confirm gate")
        return True

    # -- 6. source: the flag-driven decision cascade for the chosen source ------------
    def run_source(ctx: _Ctx) -> Any:
        evaluation = ctx.evaluate
        # (1) reference + query URL: the same source -- a same-origin data API only when the
        # page is an SPA shell backed by it, otherwise the page itself (see _source_url).
        result.reference = write_reference(evaluation, wc=wc)
        query_url = _source_url(evaluation)
        doc = wc.fetch(query_url, browser=_mode(browser), optional=True)
        artifacts.query_doc = doc
        flags = _read_flags(doc) if doc.ok else {}
        return {"url": query_url, "doc": doc, "flags": flags}

    def source_gate(ctx: _Ctx, src: Any) -> "bool | str":
        flags = src["flags"]
        # (2) a login wall on the source itself -> no query reaches the data; stop.
        if flags.get("login_required") is not None and flags["login_required"].present:
            return stop("the source requires login")
        # (3) resolve policy: spa -> browser, anti_bot_triggered -> proxy/stealth.
        result.resolve = write_resolve(list(flags.values()))
        fired = [n for n, f in flags.items() if f.present]
        note("flags fired: %s; authoring the query", ", ".join(fired) or "none")
        return True

    # -- 7. query: authored from the skeleton; the reviews are flags -------------------
    def run_query(ctx: _Ctx) -> Any:
        src, evaluation = ctx.source, ctx.evaluate
        doc = src["doc"]
        # (4) the source was already fetched above (for the flags) -- reuse that Document so
        # write_query doesn't re-fetch (a browser/proxy re-fetch is real budget + latency).
        q = write_query(
            src["url"], brief, wc=wc, llm=llm, browser=_mode(browser),
            paginated=evaluation.has_pagination, resolve=result.resolve,
            doc=doc if doc.ok else None,
            recency=_recency_guidance(evaluation),  # sort order + where the most recent records are
            author_engine=author_engine,  # "text" (write query code) | "index" (pick indexes -> build_query)
        )
        result.query = q
        if isinstance(llm, LlmClient):
            result.cost_usd = llm.spent_usd
        # a real success EXTRACTS data with every required field: a query that ran but produced
        # 0 rows, or left a required field empty on every row, is NOT ok (a fallback `best`
        # artifact is kept for the summary but is marked not-complete).
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
        return q

    def query_review(ctx: _Ctx, q: Any) -> Any:
        # the query review + the deterministic timeliness assessment are FLAGS for the human
        # (recorded on result.reviews), not gates: a working query is never discarded because a
        # model graded it low or the data looks stale.
        if not review or q is None:
            return None
        r = review_query(result, artifacts, brief, llm=llm)
        _note_review(result, r)
        # the brief's own EXIT CONDITION, re-checked against the query RESULT (not just the page)
        if brief.exit_when:
            _note_review(result, review_query_exit(result, artifacts, brief, llm=llm))
        return r

    stages = [
        Stage("search", run=run_search, gate=lambda c, seeds: bool(seeds) or stop("no search seeds")),
        Stage("crawl", run=run_crawl, review=crawl_review),
        Stage("select", run=run_select, gate=lambda c, cands: bool(cands) or stop("no candidate pages")),
        Stage("evaluate", run=run_evaluate, gate=evaluate_gate, review=select_review),
        *([Stage("confirm", run=run_confirm, gate=confirm_gate)] if interactive else []),
        Stage("source", run=run_source, gate=source_gate),
        Stage("query", run=run_query, review=query_review),
    ]
    return Pipeline("onboarding", stages, bus=getattr(wc, "bus", None), propagate=(BudgetExceeded,))


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
