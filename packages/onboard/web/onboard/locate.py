"""Locate -- ``goal -> Reference``: find WHERE the dataset lives and hand Author the hints.

The phases (each skippable by supplying its output on the brief):
  1. **search**  -- turn the goal into seed URLs (an injected :class:`Search`; skipped if
     ``seeds`` given). No web-search transport lives in these packages, so search is pluggable.
  2. **crawl**   -- reach candidate pages from the seeds (:mod:`web.crawl`; skipped if
     ``candidates`` given).
  3. **evaluate**-- score every candidate with the WHOLE detection surface: :func:`web.resolve.flags`
     (noisy-OR conclusions over :mod:`web.resolve.signals`) + :meth:`Document.records`.
  4. **select**  -- the best-scoring candidate that actually holds the dataset.
  5. **prefer API** -- if a same-origin JSON data-API backs the page AND its data is consistent
     with the page, root the Reference at THAT endpoint (JSON is cleaner than scraping HTML).

Deterministic and LLM-free; a model, if any, plugs in only as the ``search`` callable.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable
from urllib.parse import urlparse

from web.crawl import Crawler, Goal
from web.fetch import NetworkEvent, Request
from web.parse import Document, parse
from web.resolve import Flag, Resolver, flags

from .models import LocateBrief, Reference

#: URL markers of API DOCUMENTATION / dev portals -- never a scrapable dataset, even if crawled.
_DOCS = (
    "/docs",
    "/documentation",
    "/developer",
    "/developers",
    "/api-docs",
    "/swagger",
    "/redoc",
    "/reference/",
    "/help/",
    "/support/",
)
#: pager remedy strings, in the order they win (a real pager beats scroll beats a cursor guess).
_PAGERS = (("paginated", "paginate"), ("infinite_scroll", "paginate:scroll"))


@runtime_checkable
class Search(Protocol):
    """Turn a goal into seed URLs. Injected (an LLM / a search API / a fixed list) so Locate stays
    transport-agnostic and offline-testable; everything above depends on this, not a vendor.
    """

    async def __call__(self, goal: str) -> list[str]: ...


def _is_docs(url: str) -> bool:
    low = url.lower()
    return any(m in low for m in _DOCS)


def _present(doc: Document, by: "dict[str, Flag]") -> bool:
    """Whether the dataset is plausibly ON this page: a JSON doc IS data, else a repeating record
    region / structured data / a data island must be present."""
    if doc.kind == "json":
        return True
    return bool(doc.records(top_k=1)) or "structured_data" in by or "data_api" in by


def _score(doc: Document, by: "dict[str, Flag]") -> float:
    """A deterministic dataset-likeness score (see :mod:`.evaluate` learnings): dataset-present x
    scrapability, minus gates that block extraction. Higher is a better source."""
    if "auth_required" in by:  # a login wall -- no query reaches the dataset
        return -1.0
    if _is_docs(doc.url):  # API docs are never the data source
        return -1.0
    if not _present(doc, by):
        return 0.0
    if doc.kind == "json":  # a served JSON/feed document is the dataset itself
        return 9.0
    regions = doc.records(top_k=1)
    base = 4.0 + (regions[0].score * 0.1 if regions else 0.0)
    if "structured_data" in by:
        base += 2.0  # machine-readable data about itself -- cleaner to extract
    if "data_api" in by:
        base += 1.0
    if "empty" in by:
        base -= 3.0
    if "blocked" in by:
        base -= 1.0
    return base


def _record_selector(doc: Document) -> "str | None":
    regions = doc.records(top_k=1)
    return regions[0].item_selector if regions else None


def _pagination(by: "dict[str, Flag]") -> "str | None":
    for name, remedy in _PAGERS:
        if name in by:
            return remedy
    return None


def data_api_endpoints(doc: Document) -> list[str]:
    """Same-origin JSON/data-API URLs the page points at: declared JSON/feed ``<link>``s, then any
    ``/api/``- or ``.json``-looking link. Deterministic (the DOM only, no live-XHR capture needed),
    ordered most-declared first, deduped."""
    host = urlparse(doc.url).hostname
    out: list[str] = []
    meta = doc.metadata()
    declared = list(meta.feeds)
    for el in doc.select_all("link[rel=alternate][type*=json], link[type*=json]"):
        href = el.attr("href")
        if href:
            declared.append(href)
    linky = [
        u
        for u in doc.links()
        if "/api/" in u.lower() or u.lower().split("?")[0].endswith(".json")
    ]
    for u in [*declared, *linky]:
        if urlparse(u).hostname == host and u not in out:
            out.append(u)
    return out


def _consistent(page: Document, api: Document) -> bool:
    """Whether ``api`` (a JSON response) BACKS ``page``: enough of its distinctive scalar leaves
    appear in the page's text. Guards against preferring an unrelated feed / a stale endpoint.
    """
    if api.kind != "json":
        return False
    leaves = [v for v in api.json_leaves(budget=2000) if len(v) >= 3][:40]
    if not leaves:
        return False
    text = page.text.lower()
    hits = sum(1 for v in leaves if v.lower() in text)
    return hits >= max(2, len(leaves) // 4)


async def _observed(page: Document, resolver: Resolver) -> "list[tuple[str, Document]]":
    """The same-origin XHR/``fetch`` responses the page ACTUALLY made whose captured body is JSON --
    the live data-API behind it, not a URL guessed from the DOM. It renders through the CALLER's
    resolver (:meth:`Resolver.snapshot`), so it captures a network stream only when the caller chose
    a browser profile; an HTTP-only resolver emits no network events and this returns ``[]`` (Locate
    never launches a browser on its own). Bodies are already drained onto the Snapshot -- no re-fetch.
    """
    snap = await resolver.snapshot(Request(url=page.url))
    host = urlparse(page.url).hostname
    out: "list[tuple[str, Document]]" = []
    seen: set[str] = set()
    for ev in snap.events:
        if not isinstance(ev, NetworkEvent) or ev.resource_type not in ("xhr", "fetch"):
            continue
        if not ev.body or ev.url in seen or urlparse(ev.url).hostname != host:
            continue
        seen.add(ev.url)
        doc = parse(ev.body, url=ev.url)
        if doc.kind == "json":
            out.append((ev.url, doc))
    return out


async def _prefer_api(page: Document, ref: Reference, resolver: Resolver) -> Reference:
    """Apply the XHR-preference rule: root the Reference at a same-origin JSON data-API that is
    consistent with the page, instead of the page. Never a blind/hallucinated pick -- the endpoint's
    data is checked against the page first. Two passes: the cheap DOM-declared/link endpoints
    (resolved), then -- only when the page is JS-gated (``needs_browser``, so its data is loaded by
    script, not in the static DOM) -- the real network stream it fires in a browser (:func:`_observed`,
    bodies already captured). The first consistent JSON source wins. A plain static page never
    launches a browser here."""
    for url in data_api_endpoints(page):
        api = await resolver.resolve(Request(url=url))
        if _consistent(page, api):
            return ref.model_copy(
                update={"url": url, "kind": api.kind, "api_endpoint": url}
            )
    if (
        ref.needs_browser
    ):  # a JS-gated page: mine the XHR/fetch stream for the API that feeds it
        for url, api in await _observed(page, resolver):
            if _consistent(page, api):
                return ref.model_copy(
                    update={"url": url, "kind": api.kind, "api_endpoint": url}
                )
    return ref


def _reference(doc: Document, by: "dict[str, Flag]") -> Reference:
    """Build the Reference for a chosen candidate from its flags + record detection (pre-API)."""
    render = {"needs_browser", "spa", "iframe"}
    return Reference(
        url=doc.url,
        kind=doc.kind,
        page_url=doc.url,
        flags=sorted(by),
        signals=sorted({s.name for f in by.values() for s in f.signals}),
        record_selector=_record_selector(doc),
        pagination=_pagination(by),
        needs_browser=any(n in by for n in render),
        detail={"score": round(_score(doc, by), 3)},
    )


async def locate(
    brief: "LocateBrief | str", *, resolver: Resolver, search: "Search | None" = None
) -> "Reference | None":
    """Find the best source for the goal and return a :class:`Reference` (or ``None`` if nothing
    holds the dataset). Pass a bare goal string for the common case. Seeds come from the brief,
    else ``search``; candidates from the brief, else a crawl; the winner is the highest dataset
    score, then the XHR/data-API preference is applied."""
    lb = LocateBrief(goal=brief) if isinstance(brief, str) else brief
    seeds = list(lb.seeds)
    if (
        lb.start_url and lb.start_url not in seeds
    ):  # a known source to seed the crawl from
        seeds.append(lb.start_url)
    if not lb.candidates and not seeds:
        if search is None:
            raise ValueError("locate needs seeds, candidates, or a search callable")
        query = (
            f"{lb.goal} {lb.search}".strip() if lb.search else lb.goal
        )  # brief search qualifier
        seeds = await search(query)

    if lb.candidates:
        docs = [await resolver.resolve(Request(url=u)) for u in lb.candidates]
    else:
        docs = [
            d
            async for d in Crawler(resolver).crawl(
                Goal(start=seeds, max_pages=lb.max_pages)
            )
        ]

    best: "Reference | None" = None
    best_page: "Document | None" = None
    best_score = 0.0
    for doc in docs:
        by = {f.name: f for f in flags(doc)}
        score = _score(doc, by)
        if score <= 0.0:
            continue
        if best is None or score > best_score:
            best, best_page, best_score = _reference(doc, by), doc, score

    if best is None or best_page is None:
        return None
    if lb.prefer_api and best_page.kind == "html":
        best = await _prefer_api(best_page, best, resolver)
    return best


__all__ = ["locate", "Search", "data_api_endpoints"]
