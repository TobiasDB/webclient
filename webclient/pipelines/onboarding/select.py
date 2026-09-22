"""onboarding.select -- see the package docstring."""


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

from .common import LLM, _MAX_PAGES_CHARS, _clip, log
from .artifacts import Brief, Candidate
from .llm import _ask_json, _fields_line
from .crawl import _is_docs_url, _is_data_doc


# --------------------------------------------------------------------------- #
# 3. select_candidates
# --------------------------------------------------------------------------- #


def select_candidates(
    crawl: Any, brief: Brief, *, llm: LLM, seed_urls: "Sequence[str]" = ()
) -> list[Candidate]:
    """Rank the crawled pages into must / should / could-evaluate candidates by
    scrapability + likely relevance to the dataset. A SEED that is itself a data document
    (a feed/JSON the caller pointed us at) is forced in as a candidate -- but only a seed,
    never every feed a site happens to expose."""
    # crawl.pages are lean PageCards by default (url / title / kind / flags projected).
    # hard-ban documentation pages: even if one was fetched (a seed / a stray pick), it
    # is never a scrapable dataset, so it can't become a candidate.
    usable = [p for p in crawl.pages if not _is_docs_url(p.final_url or p.url)]
    pages = [
        {"url": p.final_url or p.url, "title": p.title, "kind": p.kind, "flags": p.flags}
        for p in usable
    ]
    if not pages:
        return []
    rows = _ask_json(
        llm,
        render_prompt(
            "select_candidates",
            description=brief.description,
            fields_line=_fields_line(brief),
            pages_json=_clip(json.dumps(pages, indent=0), _MAX_PAGES_CHARS, "pages list", kind="json"),
        ),
    )
    out: list[Candidate] = []
    for r in rows if isinstance(rows, list) else []:
        if isinstance(r, dict) and r.get("url"):
            note = str(r.get("reason") or r.get("note") or "")  # the model's WHY
            out.append(Candidate.model_validate({**r, "url": str(r["url"]), "note": note}))
    picked = {c.url for c in out}
    # A SEED that is itself a DATA DOCUMENT (a JSON/XML feed or an API response the caller
    # pointed us at) IS the dataset -- it holds the records, it doesn't "lead to" them. The LLM
    # filter judges only url+title and routinely drops a raw feed/JSON, so force the SEED in as a
    # MUST candidate. Restricted to seeds ON PURPOSE: a site can expose many feeds (per-category,
    # comments, ...) and force-including every discovered feed would flood the candidates.
    seeds = set(seed_urls)
    for p in usable:
        u = p.final_url or p.url
        if u not in picked and _is_data_doc(p) and (u in seeds or p.url in seeds):
            out.append(Candidate(url=u, tier="must",
                                 note="a seeded data document (feed / JSON / API) — the dataset itself"))
            picked.add(u)
    # FAIL OPEN: the filter is an LLM and can return nothing on pages it should have kept
    # (variance, or a parse miss on the cheapest model). If it picked nothing yet we DID crawl
    # usable pages, keep the top few so the run still evaluates a real source rather than dying
    # at "no candidate pages" (evaluate_candidate then judges whether the dataset is actually there).
    if not out and usable:
        for p in usable[:3]:
            out.append(Candidate(url=(p.final_url or p.url), tier="could",
                                 note="fail-open: candidate filter returned nothing — kept for evaluation"))
    _rank = {"must": 0, "should": 1, "could": 2}
    out.sort(key=lambda c: _rank.get(c.tier, 3))
    for c in out:
        log.info("    candidate [%s] %s%s", c.tier, c.url, f"  — {c.note}" if c.note else "")
    return out


