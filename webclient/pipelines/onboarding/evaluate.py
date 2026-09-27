"""onboarding.evaluate -- see the package docstring."""


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

from .common import LLM, BrowserMode, _fetch, _skeleton_for, log
from .artifacts import Brief, Candidate, CandidateEval
from .llm import _ask_json, _fields_line, _read_flags


# --------------------------------------------------------------------------- #
# 4. evaluate_candidate
# --------------------------------------------------------------------------- #


def evaluate_candidate(
    candidate: Candidate,
    brief: Brief,
    *,
    wc: WebClient,
    llm: LLM,
    browser: BrowserMode = "auto",
) -> CandidateEval:
    """Fetch the candidate, read its skeleton + signals, and have the model judge the
    dataset (present, sorted, complete, paginated, filtered, a subset, unstructured,
    drill-down)."""
    doc = _fetch(wc, candidate.url, browser, optional=True)  # escalate auto->browser if blocked
    if not doc.ok:
        return CandidateEval(url=candidate.url, verdict="fetch failed")
    flags = _read_flags(doc)  # the detected conclusions (spa / pagination / login / ...)
    flag_map = {n: round(f.confidence, 2) for n, f in flags.items() if f.present}
    # the evidence behind each present flag -- the signals that fired, for the summary
    flag_signals = {
        n: [f"{s.name} ({s.stage}, {s.confidence:.2f}): {s.reason}" for s in f.signals]
        for n, f in flags.items() if f.present
    }
    # a login wall blocks the dataset -- no query reaches it; drop the candidate early.
    if flags["login_required"].present:
        return CandidateEval(url=candidate.url, verdict="login required",
                             flags=flag_map, flag_signals=flag_signals)
    # a BINARY document (PDF / image / spreadsheet) IS the deliverable -- a download, not a page to
    # scan for records. Accept it as the dataset (not queryable) without a skeleton or an LLM call.
    if getattr(doc, "kind", "html") not in ("html", "xml", "json"):
        return CandidateEval(url=candidate.url, dataset_present=True, is_queryable=False,
                             completeness="full", scrapability=6,
                             verdict=f"a {doc.kind} document (a download)",
                             flags=flag_map, flag_signals=flag_signals)
    skeleton = _skeleton_for(doc)
    endpoints = [c.url for c in doc.xhr_endpoints()]
    interactive = flags["forms"].present or flags["buttons"].present
    errs: list[str] = []  # records an LlmError so a model OUTAGE isn't read as "no dataset here"
    parsed = _ask_json(
        llm,
        render_prompt(
            "evaluate_candidate",
            description=brief.description,
            fields_line=_fields_line(brief),
            candidate_url=candidate.url,
            flag_map_json=json.dumps(flag_map),
            endpoints_json=json.dumps(endpoints),
            skeleton=skeleton,
            exit_condition=(f"EXIT CONDITION (from the brief): {brief.exit_when} If this holds "
                            "for THIS page, set exit_when_met=true with a one-line exit_reason; "
                            "the pipeline will then stop cleanly WITHOUT authoring a query.\n"
                            if brief.exit_when else ""),
        ),
        errors=errs,
    )
    if parsed is None and errs:  # the MODEL was unavailable -- a retry, NOT a judged-empty source
        return CandidateEval(url=candidate.url, verdict="the model was unavailable — page not assessed",
                             flags=flag_map, flag_signals=flag_signals, llm_unavailable=True)
    data: dict[str, Any] = dict(parsed) if isinstance(parsed, dict) else {"verdict": "could not evaluate"}
    # the flags are ground truth for structure -> they win over the model's guesses.
    data["has_pagination"] = bool(data.get("has_pagination")) or flags["pagination"].present
    data["pagination_hint"] = flags["pagination"].value  # the detected pager modes (best first) + totals, or None
    # The API endpoint is used ONLY if the model names one of the OBSERVED same-origin
    # XHR endpoints (never a blind "first XHR" pick, and never a hallucinated URL) -- so
    # the reference stays the CHOSEN page unless a real data endpoint is identified. This
    # fixes the reference pointing at a different URL than the source.
    llm_ep = data.get("api_endpoint")
    api_endpoint = llm_ep if (isinstance(llm_ep, str) and llm_ep in set(endpoints)) else None
    # an API-documentation page is never the data source -- guard even if the model was
    # inconsistent (this is the "docs page mistaken for the API" fix).
    if data.get("is_api_docs"):
        data["dataset_present"] = False
        data["is_queryable"] = False
        api_endpoint = None
    data.update(url=candidate.url, flags=flag_map, flag_signals=flag_signals,
                api_endpoint=api_endpoint, interactive=interactive)
    if not brief.exit_when:  # no exit condition -> the model's exit fields never apply
        data["exit_when_met"], data["exit_reason"] = False, ""
    ev = CandidateEval.model_validate(data)
    log.info(
        "    evaluated %s -> present=%s, queryable=%s, scrapability=%d%s — %s",
        candidate.url, ev.dataset_present, ev.is_queryable, ev.scrapability,
        " [API DOCS]" if ev.is_api_docs else "", ev.verdict or "(no reason given)",
    )
    return ev


def evaluate_candidates(
    candidates: Sequence[Candidate],
    brief: Brief,
    *,
    wc: WebClient,
    llm: LLM,
    browser: BrowserMode = "auto",
) -> CandidateEval | None:
    """Evaluate candidates best-tier-first until a usable source is found (returns
    it) or the options run out (returns the best-scoring evaluation seen, or None).
    Prefers a queryable source, but by a TIER-weighted score -- the model's ranking of which
    page IS the dataset (``must`` > ``should`` > ``could``) dominates, so a per-record DRILL-DOWN
    endpoint (a listing links to per-item ``/api/{id}`` JSON) that merely happens to be queryable
    cannot outrank the listing itself (see :func:`_candidate_score`)."""
    best: CandidateEval | None = None
    best_tier = ""
    for c in candidates:
        ev = evaluate_candidate(c, brief, wc=wc, llm=llm, browser=browser)
        if best is None or _candidate_score(ev, c.tier) > _candidate_score(best, best_tier):
            best, best_tier = ev, c.tier
        # early exit only for the IDEAL case: the TOP-ranked (must) page is itself a queryable
        # dataset. A lower-tier queryable candidate must NOT preempt -- it may be a drill-down.
        if ev.usable and ev.is_queryable and c.tier == "must":
            return ev
    return best


def _candidate_score(ev: CandidateEval, tier: str = "") -> float:
    """Rank a candidate. The model's TIER (its judgement of which page IS the dataset) is the
    PRIMARY signal, so a per-record drill-down endpoint that merely happens to be queryable can't
    outrank the listing the model marked ``must``. Within a tier: a queryable source is preferred,
    then scrapability. A source without the dataset is never preferred over one that has it."""
    if not ev.dataset_present:
        return -1.0
    tier_rank = {"must": 2, "should": 1}.get(tier, 0)  # could / unknown -> 0 (e.g. the fail-open picks)
    return tier_rank * 100 + (4 if ev.is_queryable else 0) + ev.scrapability


