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

from .models import (
    Collect,
    CrawlEvent,
    Follow,
    Frontier,
    FrontierItem,
    Goal,
    by_score,
    same_origin,
)
from .robots import Robots, parse_robots, robots
from .sitemap import sitemap_urls
from .urls import canonical


class Crawler:
    """Turn-based crawl over a :class:`~web.resolve.Resolver`, driven by a :class:`Goal`. Each round
    a policy (:attr:`Goal.frontier`, e.g. an LLM) picks which pending URLs to expand; the default is
    breadth-first (FIFO)."""

    def __init__(self, resolver: Resolver) -> None:
        self._resolver = resolver

    async def _select(self, pending: "list[FrontierItem]", goal: Goal) -> "list[FrontierItem]":
        """This round's batch to expand: the frontier policy's picks (in its order; a subset PRUNES
        the rest, ``[]`` stops), else the single oldest item (plain breadth-first)."""
        if goal.frontier is None:
            return [pending.pop(0)]
        order = await goal.frontier(tuple(pending))
        batch: list[FrontierItem] = []
        for url in order:
            hit = next((it for it in pending if it.url == url), None)
            if hit is not None:
                pending.remove(hit)
                batch.append(hit)
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

        def admit(url: str, depth: int, parent: str) -> None:  # canonical-dedup + robots gate
            key = canonical(url)
            if key in seen or (rob is not None and not rob.allowed(url)):
                return
            seen.add(key)
            pending.append(FrontierItem(url=url, depth=depth, parent=parent))

        for s in seeds:  # seeds sit UNFETCHED in the frontier -- a policy can evaluate/prune them
            admit(s, 0, "")

        yielded: set[str] = set()
        fetched = 0
        while pending and fetched < goal.max_pages:
            batch = await self._select(pending, goal)
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
                if snap.error is not None:  # transport failure: nothing to yield or traverse
                    continue
                if goal.collect is None or goal.collect(doc):
                    key = canonical(
                        _canonical_url(doc)
                    )  # the page's OWN identity (rel=canonical wins)
                    if key not in yielded:
                        yielded.add(key)
                        yield doc
                if doc.kind in ("html", "xml"):  # traverse links even from non-results
                    for link in doc.links():
                        if goal.scope(doc, link):
                            admit(link, item.depth + 1, item.url)

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
    "Frontier",
    "FrontierItem",
    "by_score",
    "same_origin",
    "canonical",
    "robots",
    "parse_robots",
    "Robots",
    "sitemap_urls",
]
