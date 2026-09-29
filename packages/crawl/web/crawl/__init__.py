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

from collections import deque
from collections.abc import AsyncIterator
from urllib.parse import urlparse

from web.fetch import Request
from web.fetch import emit
from web.parse import Document
from web.resolve import Resolver

from .models import Collect, CrawlEvent, Follow, Goal, same_origin
from .robots import Robots, parse_robots, robots
from .sitemap import sitemap_urls
from .urls import canonical


class Crawler:
    """Breadth-first crawl over a :class:`~web.resolve.Resolver`, driven by a :class:`Goal`."""

    def __init__(self, resolver: Resolver) -> None:
        self._resolver = resolver

    async def crawl(self, goal: Goal) -> AsyncIterator[Document]:
        """Resolve the goal's entry point(s) and their in-scope links breadth-first, yielding each
        RESULT document once, until the frontier drains or ``max_pages`` pages are fetched. URLs are
        deduped by :func:`~web.crawl.urls.canonical` (so ``/p`` and ``/p?utm=x`` are one page), and
        a result that declares a ``rel=canonical`` already yielded is not yielded again."""
        seeds = [goal.start] if isinstance(goal.start, str) else list(goal.start)
        seen: set[str] = set()
        frontier: deque[str] = deque()

        rob: "Robots | None" = None
        if goal.respect_robots and seeds:
            rob = await robots(self._resolver, seeds[0])
        if goal.sitemap or (rob and rob.sitemaps):
            for sm in _sitemap_sources(seeds, rob):
                seeds.extend(await sitemap_urls(self._resolver, sm))

        def admit(url: str) -> bool:  # canonical-dedup + robots gate, in one place
            key = canonical(url)
            if key in seen or (rob is not None and not rob.allowed(url)):
                return False
            seen.add(key)
            return True

        for s in seeds:  # the frontier is internal -- derived from the goal
            if admit(s):
                frontier.append(s)

        yielded: set[str] = set()
        fetched = 0
        while frontier and fetched < goal.max_pages:
            url = frontier.popleft()
            doc = await self._resolver.resolve(Request(url=url))
            fetched += 1
            emit(CrawlEvent(url=url, fetched=fetched))
            if goal.collect is None or goal.collect(doc):
                key = canonical(_canonical_url(doc))  # the page's OWN identity (rel=canonical wins)
                if key not in yielded:
                    yielded.add(key)
                    yield doc
            if doc.kind in ("html", "xml"):  # traverse links even from non-results
                for link in doc.links():
                    if goal.scope(doc, link) and admit(link):
                        frontier.append(link)

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


__all__ = ["Crawler", "Goal", "CrawlEvent", "Follow", "Collect", "same_origin",
           "canonical", "robots", "parse_robots", "Robots", "sitemap_urls"]
