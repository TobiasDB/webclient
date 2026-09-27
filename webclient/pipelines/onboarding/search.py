"""onboarding.search -- see the package docstring."""


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

from .common import LLM, SearchFn, _MAX_LISTING_CHARS, _clip, log
from .artifacts import Brief, SearchHit, Seed
from .llm import _ask_json, _fields_line


# --------------------------------------------------------------------------- #
# 1. search_web
# --------------------------------------------------------------------------- #


def ddg_search(query: str, k: int = 6) -> "list[SearchHit]":
    """A default :data:`SearchFn` backed by the ``ddgs`` package (DuckDuckGo and
    friends). Optional dependency -- raises a clear error if it is not installed;
    inject your own ``search`` callable to avoid it."""
    try:
        from ddgs import DDGS  # type: ignore[import-not-found, unused-ignore]  # optional dep
    except ImportError as exc:  # pragma: no cover - exercised only without the dep
        raise RuntimeError(
            "web search needs the 'ddgs' package (pip install ddgs), or pass your "
            "own search=... callable to search_web()/onboard_company()."
        ) from exc
    # `backend` names the search engine(s) ddgs queries: "auto" lets it fall through its list
    # (duckduckgo, google, bing, brave, ...). Override with the WEBCLIENT_SEARCH_BACKEND env var.
    backend = os.environ.get("WEBCLIENT_SEARCH_BACKEND", "auto")
    try:
        with DDGS() as ddgs:
            rows = ddgs.text(query, max_results=k, backend=backend) or []
    except Exception as exc:  # noqa: BLE001 - ddgs raises on rate limits / no results / timeouts;
        # a search backend hiccup is not fatal -- return no seeds so the caller can retry with a
        # different term (see search_web) instead of crashing the whole onboarding run.
        log.info("    web search error for %r (backend=%s): %s -- treating as no results",
                 query, backend, exc)
        return []
    log.info("    web search via ddgs (backend=%s): %d result(s) for %r", backend, len(rows), query)
    return [
        SearchHit(
            url=str(r.get("href") or r.get("url") or ""),
            title=str(r.get("title") or ""),
            snippet=str(r.get("body") or r.get("snippet") or ""),
        )
        for r in rows
        if r.get("href") or r.get("url")
    ]


def _apply_search_term(company: str, term: str) -> str:
    """``"<company> <term>"``, or ``term`` with a literal ``{company}`` placeholder filled in.
    Empty ``term`` -> the bare company name."""
    term = term.strip()
    if not term:
        return company.strip()
    if "{company}" in term:
        return term.replace("{company}", company).strip()
    return f"{company} {term}".strip()


def _search_query(brief: Brief, company: str, *, mode: str = "normal") -> str:
    """The web-search query for ``company`` -- DETERMINISTIC, from the brief's ``search``
    qualifier: ``"<company> <brief.search>"`` (e.g. ``"Adobe investor relations news"``).
    ``mode`` steers a retry: ``"broaden"`` drops the qualifier for the bare company name (plus a
    site-type hint); ``"disambiguate"`` adds a distinguishing hint when a look-alike came back."""
    if mode == "broaden":  # got nothing -- simplest possible: name + one site-type hint
        hint = (brief.look[0] if brief.look else "") or ""
        return f"{company} {hint}".strip() or company.strip()
    query = _apply_search_term(company, brief.search)
    if mode == "disambiguate":  # a look-alike came back -- add a distinguishing hint
        hint = (brief.look[0] if brief.look else "") or brief.title
        query = f"{query} {hint}".strip()
    return query or company.strip()


def _seeds_for_company(seeds: "list[Seed]", company: str, brief: Brief, llm: "LLM | None") -> "list[Seed]":
    """Keep only the seeds that actually belong to ``company`` -- the model rejects
    look-alike companies with a similar name (``Square`` when we asked for ``Squarepoint``),
    unrelated orgs and aggregators. Fails OPEN: if the model gives no usable judgement, all
    seeds are kept (never silently drop everything on a bad reply)."""
    if llm is None or not seeds:
        return list(seeds)
    listing = "\n".join(f"{i}. {s.url}  [{s.title}]  {s.why}"[:300] for i, s in enumerate(seeds))
    data = _ask_json(llm, render_prompt(
        "verify_seeds", company=company, description=brief.description,
        fields_line=_fields_line(brief),  # the brief guides every decision, this one included
        seeds=_clip(listing, _MAX_LISTING_CHARS, "seed results"),
    ))
    belong = data.get("belong") if isinstance(data, dict) else None
    if not isinstance(belong, list):
        return list(seeds)  # fail open -- no usable judgement
    keep = {i for i in belong if isinstance(i, int)}
    kept = [s for i, s in enumerate(seeds) if i in keep]
    for i, s in enumerate(seeds):
        if i not in keep:
            log.info("    dropped off-company seed: %s [%s]", s.url, s.title)
    return kept


def search_web(
    brief: Brief,
    company: str,
    *,
    search: SearchFn,
    k: int = 6,
    llm: LLM | None = None,
) -> list[Seed]:
    """Seed URLs for ``company`` + ``brief``. The query is DETERMINISTIC -- ``"<company>
    <brief.search>"`` (the brief's ``search`` qualifier). Pass ``llm`` to verify each result
    really belongs to ``company`` (dropping look-alike companies with a similar name). If the
    whole first result set is the wrong company, the search retries with a disambiguating hint;
    no results -> it broadens to the bare company name. FAIL-OPEN: if verification would drop
    EVERY result on both tries, the raw results are returned anyway rather than sinking the
    company on a stubborn/erroneous LLM judgement."""
    raw: list[Seed] = []
    mode = "normal"
    tried: set[str] = set()
    for _attempt in range(3):
        query = _search_query(brief, company, mode=mode)
        if query in tried:  # don't re-issue the same query -- force a simpler, different one
            query = f"{company} {(brief.look[0] if brief.look else brief.name or brief.title)}".strip() or company
        tried.add(query)
        log.info("    search query: %r", query)
        seeds = [Seed(url=h.url, title=h.title, why=h.snippet) for h in search(query, k) if h.url]
        if not seeds:  # NO results (empty, or a backend error) -- BROADEN and retry a different term
            log.info("    no results for %r -- retrying the search, broader", query)
            mode = "broaden"
            continue
        raw = raw or seeds  # remember the first non-empty result set for the fail-open path
        kept = _seeds_for_company(seeds, company, brief, llm)
        if kept:
            if len(kept) < len(seeds):
                log.info("    %d/%d result(s) belong to %s", len(kept), len(seeds), company)
            return kept
        # results came back but none were this company -- try a stricter, disambiguating query
        log.info("    no result belongs to %s -- retrying the search, stricter", company)
        mode = "disambiguate"
    if raw:  # verification killed everything -- crawl the raw seeds rather than give up
        log.info("    verification dropped all results for %s -- using the raw seeds", company)
    return raw


