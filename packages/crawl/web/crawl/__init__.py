"""web.crawl -- reach: a :class:`Goal` -> Documents.

The input is a GOAL -- what to crawl: where to enter, how far to range, which links to follow,
and which documents count as results. The frontier (the "seeds"/queue) is an internal concept
derived from the Goal. A breadth-first walk over the resolve layer resolves a page, follows its
in-scope links, and yields the documents that match the goal, bounded and deduplicated -- streamed
as an async generator so a caller can consume and stop.

    from web.resolve import Resolver
    from web.crawl import Crawler, Goal
    async for doc in Crawler(Resolver()).crawl(Goal(start="https://site.com")):
        ...
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from web.fetch import Request, emit
from web.parse import Document
from web.resolve import Resolver, document, flags

from .frontier import FrontierMiddleware, Select, fifo
from .frontier import stack as _frontier
from .models import Collect, CrawlEvent, Follow, FrontierItem, Goal, same_origin
from .robots import Robots, parse_robots, robots
from .sitemap import sitemap_urls
from .urls import canonical


class Crawler:
    """Turn-based crawl over a :class:`~web.resolve.Resolver`, driven by a :class:`Goal`. Each round
    a policy (:attr:`Goal.frontier`, e.g. an LLM) picks which pending URLs to expand; the default is
    breadth-first (FIFO)."""

    def __init__(self, resolver: Resolver) -> None:
        self._resolver = resolver

    async def _select(self, pending: "list[FrontierItem]", select: Select) -> "list[FrontierItem]":
        """This round's batch to expand -- the frontier middleware chain's picks (a subset PRUNES the
        rest, ``[]`` stops); the returned items are removed from the frontier. Default chain = FIFO.
        """
        batch = list(await select(tuple(pending)))
        for it in batch:
            hit = next((p for p in pending if p.url == it.url), None)
            if hit is not None:
                pending.remove(hit)
        return batch

    async def crawl(self, goal: Goal) -> AsyncIterator[Document]:
        """Resolve the goal's entry point(s) and their in-scope links, yielding each RESULT document
        once, until the frontier drains, the policy stops it, or ``max_pages`` pages are fetched.
        Each round a policy picks which frontier URLs to expand (best-first / LLM-driven), else FIFO.
        URLs are deduped by :func:`~web.crawl.urls.canonical` (so ``/p`` and ``/p?utm=x`` are one
        page), and a result declaring an already-yielded ``rel=canonical`` is not yielded again. A
        page that fails at the transport level does not abort the crawl -- it is reported (``ok`` is
        False) and skipped. With ``goal.assess`` each page's flags ride on its :class:`CrawlEvent`.
        """
        seeds = [goal.start] if isinstance(goal.start, str) else list(goal.start)
        seen: set[str] = set()
        pending: list[FrontierItem] = []

        rob: "Robots | None" = None
        if goal.respect_robots and seeds:
            rob = await robots(self._resolver, seeds[0])
        if goal.sitemap or (rob and rob.sitemaps):
            for sm in _sitemap_sources(seeds, rob):
                seeds.extend(await sitemap_urls(self._resolver, sm))

        def admit(url: str, item: FrontierItem) -> None:  # canonical-dedup + robots gate
            key = canonical(url)
            if key in seen or (rob is not None and not rob.allowed(url)):
                return
            seen.add(key)
            pending.append(item)

        for s in seeds:  # seeds sit UNFETCHED in the frontier -- a policy can evaluate/prune them
            admit(s, FrontierItem(url=s))

        select = _frontier(fifo, goal.frontier)  # the frontier middleware chain (default: FIFO)
        yielded: set[str] = set()
        fetched = 0
        while pending and fetched < goal.max_pages:
            batch = await self._select(pending, select)
            if not batch:  # the policy pruned everything -> stop
                break
            for item in batch:
                if fetched >= goal.max_pages:
                    break
                snap = await self._resolver.snapshot(Request(url=item.url))
                doc = document(snap)
                fetched += 1
                fired = [f.name for f in flags(doc, snap)] if goal.assess else []
                emit(
                    CrawlEvent(
                        url=item.url, fetched=fetched, status=snap.status, ok=snap.ok, flags=fired
                    )
                )
                if not snap.ok:  # a transport failure or a bad status (403/404/5xx): not a source
                    continue  # reported on the event above; never yielded or traversed
                if goal.collect is None or goal.collect(doc):
                    key = canonical(
                        _canonical_url(doc)
                    )  # the page's OWN identity (rel=canonical wins)
                    if key not in yielded:
                        yielded.add(key)
                        yield doc
                if doc.kind in ("html", "xml"):  # traverse links from OK pages
                    meta = doc.metadata()
                    title = meta.title or meta.description or ""
                    for link, text in doc.anchors():  # carry the PARENT's assessment onto each edge
                        if goal.scope(doc, link):
                            admit(
                                link,
                                FrontierItem(
                                    url=link,
                                    depth=item.depth + 1,
                                    parent=item.url,
                                    text=text,
                                    parent_status=snap.status,
                                    parent_title=title,
                                    parent_flags=fired,
                                ),
                            )

    async def aclose(self) -> None:
        await self._resolver.aclose()


def _canonical_url(doc: Document) -> str:
    """A result's canonical identity: its declared ``rel=canonical``, else its own URL."""
    if doc.kind == "html":
        declared = doc.metadata().canonical
        if declared:
            return declared
    return doc.url


def _sitemap_sources(seeds: list[str], rob: "Robots | None") -> list[str]:
    """Where to pull sitemaps from: the ones robots advertises, else the first seed's origin."""
    if rob and rob.sitemaps:
        return list(rob.sitemaps)
    return [seeds[0]] if seeds else []


__all__ = [
    "Crawler",
    "Goal",
    "CrawlEvent",
    "Follow",
    "Collect",
    "FrontierItem",
    "FrontierMiddleware",
    "Select",
    "fifo",
    "same_origin",
    "canonical",
    "robots",
    "parse_robots",
    "Robots",
    "sitemap_urls",
]
