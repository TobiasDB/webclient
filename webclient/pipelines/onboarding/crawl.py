"""onboarding.crawl -- see the package docstring."""


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

from .common import LLM, _MAX_LISTING_CHARS, _clip, log
from .artifacts import Brief, Seed
from .llm import _ask_json, _fields_line


# --------------------------------------------------------------------------- #
# 2. crawl_from_seeds  (LLM-driven frontier)
# --------------------------------------------------------------------------- #


def _frontier_key(url: str) -> "tuple[str, str, frozenset[str]]":
    """A dedup key that collapses a paginated set and repeated calls to one API: the
    host + the path with any ``/page/N`` segment stripped + the set of query-param
    KEYS (ignoring their values). So ``?page=1`` / ``?page=2`` and ``/list/page/3``
    collapse to one, while distinct resources (``/item/1`` vs ``/item/2``) stay apart."""
    import re
    from urllib.parse import parse_qsl, urlsplit

    parts = urlsplit(url)
    path = re.sub(r"/(?:page|p)/\d+", "", parts.path).rstrip("/") or "/"
    # ignore pagination params so ?page=1 / ?page=2 / /page/3 all collapse together
    keys = frozenset(
        k for k, _ in parse_qsl(parts.query) if k.lower() not in _PAGE_PARAMS
    )
    return (parts.netloc, path, keys)


#: query params that only page/window a result set (not a distinct resource).
_PAGE_PARAMS = {
    "page", "p", "pg", "pagenum", "offset", "start", "limit", "per_page", "cursor",
}

#: path fragments that mark a page as API / product DOCUMENTATION rather than data. A
#: docs page is never a scrapable dataset, so it is HARD-BANNED from the crawl (never
#: expanded, never a candidate) -- kept specific so a data endpoint like ``/api/v1/
#: products`` is NOT caught (only ``/api-docs`` etc.).
_DOCS_HINTS = (
    "/docs", "/doc/", "/documentation", "/developer", "/dev-docs", "/api-docs",
    "/apidocs", "/api-reference", "/reference/", "/swagger", "/openapi", "/redoc",
    "/guide", "/tutorial", "/sdk", "/faq", "/help/", "/knowledge", "/manual",
)


def _is_docs_url(url: str) -> bool:
    """Whether ``url`` is API / product DOCUMENTATION (a ``docs.`` / ``developer.``
    host, or a docs path fragment) -- hard-banned from the crawl."""
    from urllib.parse import urlsplit

    parts = urlsplit(url.lower())
    host = parts.hostname or ""
    if host.startswith(("docs.", "developer.", "developers.", "apidocs.")):
        return True
    path = parts.path.rstrip("/") + "/"  # so a trailing "/docs" matches "/docs/"
    return any(hint in path for hint in _DOCS_HINTS)


#: URL fragments that mark a feed / data endpoint (a page that IS the dataset, not one
#: that links to it) -- used alongside the sniffed ``kind`` so a served-as-text feed counts.
_DATA_DOC_HINTS = (".rss", ".atom", "/rss", "/feed", "/atom", ".json", "/api/", "/api.")


def _is_data_doc(card: Any) -> bool:
    """Whether a crawled page IS a data document -- a JSON/XML body (sniffed ``kind``), or a
    feed/API URL. Such a page holds the dataset directly, so it should be a candidate outright
    rather than left to the LLM filter (which judges only url+title and tends to drop it)."""
    if getattr(card, "kind", "html") in ("json", "xml"):
        return True
    u = (getattr(card, "final_url", None) or card.url).lower()
    return any(h in u for h in _DATA_DOC_HINTS)


def _reg_domain(url: str) -> str:
    """The registrable domain (eTLD+1) of ``url`` -- the identity we bind the crawl to,
    so ``news.adobe.com`` / ``www.adobe.com`` / ``milo.adobe.com`` all read as ``adobe.com``."""
    from urllib.parse import urlsplit

    from ...core.crawl.canon import _registrable

    return _registrable((urlsplit(url).hostname or "").lower())


def _seed_domains(seeds: Sequence[Seed]) -> "set[str]":
    """The registrable domains the search surfaced for THIS company -- the company's web
    footprint. The crawl is kept inside it so it can't wander onto a different company's
    site discovered mid-crawl (the search query was ``"<company> <brief>"``, so the seed
    domains are the company's own)."""
    return {d for s in seeds if s.url and (d := _reg_domain(s.url))}


def _filter_frontier(
    edges: Sequence[Any], brief: Brief, *, allow_domains: "frozenset[str] | set[str]" = frozenset()
) -> list[Any]:
    """Prune the frontier before the model spends a pick on it: keep only the company's
    own domains (``allow_domains`` -- so it can't drift onto an unrelated company), HARD-BAN
    documentation pages (:func:`_is_docs_url` -- never a dataset), then collapse paginated
    URL sets and repeated similar-API calls to one representative each (keeping the first --
    the frontier is already best-first). ``look`` / ``ignore`` stay natural-language guides
    the model applies; this filter is purely structural."""
    kept: list[Any] = []
    seen: set[tuple[str, str, frozenset[str]]] = set()
    for e in edges:
        if allow_domains and _reg_domain(e.url) not in allow_domains:
            log.debug("off-company URL dropped from the frontier: %s", e.url)
            continue
        if _is_docs_url(e.url):
            log.debug("banned docs URL from the frontier: %s", e.url)
            continue
        key = _frontier_key(e.url)
        if key in seen:
            continue
        seen.add(key)
        kept.append(e)
    return kept


def _pick_edges(llm: LLM, brief: Brief, frontier: Sequence[Any], *, company: str = "") -> list[str]:
    """Ask the model which frontier edges to expand next -- the ones most likely to
    reach the dataset, preferring a queryable source (an API over the whole dataset)
    to a page that lists only part of it, and following pagination when it must.
    ``company`` is named so the model stays on that company's pages and skips any that
    belong to a different organisation."""
    listing = _clip(
        "\n".join(f"{i}. {e.url}   (link text: {e.text!r})" for i, e in enumerate(frontier)),
        _MAX_LISTING_CHARS, "frontier listing",
    )
    picked = _ask_json(
        llm,
        render_prompt(
            "pick_edges",
            company=company or "the company",
            description=brief.description,
            fields_line=_fields_line(brief),
            listing=listing,
        ),
    )
    if not isinstance(picked, list):
        return []
    urls: list[str] = []
    for item in picked:
        # accept {"n": i, "why": "..."} (reasoned) or a bare index for robustness
        if isinstance(item, dict):
            idx, why = item.get("n", item.get("index")), str(item.get("why") or item.get("reason") or "")
        else:
            idx, why = item, ""
        if isinstance(idx, int) and 0 <= idx < len(frontier):
            urls.append(frontier[idx].url)
            log.info("    pick %s%s", frontier[idx].url, f"  — {why}" if why else "")
    return urls


def crawl_from_seeds(
    seeds: Sequence[Seed],
    brief: Brief,
    *,
    wc: WebClient,
    llm: LLM,
    company: str = "",
    max_pages: int = 20,
    rounds: int = 4,
    browser: bool = True,
) -> Any:
    """A hand-driven crawl steered by the model: fetch the seeds, then each round let
    the model pick which discovered edges to expand (favouring a queryable source),
    up to ``rounds`` rounds or ``max_pages`` pages. Returns the finished ``Crawl``
    (read ``.pages`` for the retained Documents). The brief's ``crawl`` block overrides
    ``max_pages`` / ``rounds`` / ``depth`` / ``browser`` per dataset. The crawl is bound
    to the company's own domains (the seed footprint) so it can't wander onto a different
    company's site."""
    cfg = brief.crawl
    max_pages = int(cfg.get("max_pages", max_pages))
    rounds = int(cfg.get("rounds", rounds))
    browser = cfg.get("browser", browser)  # bool or a tier ("auto"/"always"/"never")
    depth = int(cfg.get("depth", 3))
    seed_urls = [s.url for s in seeds if s.url]
    domains = _seed_domains(seeds)  # the company's web footprint -- the crawl stays inside it
    # The seed URLs the search surfaced, printed BEFORE the model filters/picks them.
    log.info("    %d seed URL(s) for %s (domains: %s):",
             len(seed_urls), company or "the company", ", ".join(sorted(domains)) or "?")
    for s in seeds:
        if s.url:
            log.info("      seed %s%s", s.url, f"  — {s.title}" if s.title else "")
    # The LLM edge-picker is a crawl DRIVER (see webclient.core.crawl.drivers): each round
    # the crawl hands it the frontier, it filters to the company's domains + collapses
    # dup/docs pages, then the model picks which edges reach the dataset. The driving loop
    # (list frontier -> pick -> expand) lives in the crawl; only the policy lives here.
    def _pick(edges: "Sequence[Any]") -> list[str]:
        candidates = _filter_frontier(list(edges), brief, allow_domains=domains)
        return _pick_edges(llm, brief, candidates, company=company) if candidates else []

    crawl = wc.crawl(
        seed_urls, auto=False, browser=browser, max_pages=max_pages, depth=depth,
        obey_robots=False, allow_domains=sorted(domains), driver=from_picks(_pick),
    )
    seen_pages = seen_fails = 0
    # The seeds sit in the frontier UNFETCHED: round 0 lets the model evaluate the seeds
    # and pick which to fetch (not blindly fetch them all), and each further round picks
    # from newly-discovered edges -- so a docs/irrelevant seed is dropped before it costs
    # a fetch. If the model rejects every seed on round 0, fall back to the filtered seeds
    # so the crawl still gets off the ground.
    for round_i in range(rounds + 1):
        if not crawl.frontier or len(crawl.pages) >= max_pages:
            break
        before = len(crawl.pages)
        crawl.step()  # the driver: filter -> model pick -> expand this round's edges
        if len(crawl.pages) == before and round_i == 0:
            candidates = _filter_frontier(list(crawl.frontier), brief, allow_domains=domains)
            if not candidates:
                break
            log.info("    model chose no frontier links this round — fetching the seed(s) directly")
            crawl.step([e.url for e in candidates])
        if len(crawl.pages) == before:
            break  # no progress this round -- stop
        seen_pages, seen_fails = _log_crawl_progress(crawl, seen_pages, seen_fails)
    return crawl


def _log_crawl_progress(crawl: Any, seen_pages: int, seen_fails: int) -> "tuple[int, int]":
    """Log each newly-fetched URL since the last round with the TRANSPORT it used (http vs
    browser -- from the page's final tier) and the SIGNALS that fired on it (the flag
    names), plus each failed edge + why. So the crawl is observable page by page: you can
    see when a page forced a browser escalation and what it tripped."""
    for card in crawl.pages[seen_pages:]:
        # the FULL resolve trail, so it's explicit whether http was enough or a browser (and
        # any proxy) was needed: "http" (static only) / "http→browser" / "http→proxy→browser".
        esc = getattr(card, "escalation", None) or [getattr(card, "final_tier", "static") or "static"]
        transport = "→".join("http" if t == "static" else t for t in esc)
        flags = ", ".join(getattr(card, "flags", []) or []) or "none"
        log.info("    crawl [%s] %s  (via %s; signals: %s)",
                 card.status_code, card.final_url or card.url, transport, flags)
    for fail in crawl.failures[seen_fails:]:
        log.info("    crawl [%s] %s  (%s)", fail.status_code or "x", fail.url, fail.reason)
    return len(crawl.pages), len(crawl.failures)


