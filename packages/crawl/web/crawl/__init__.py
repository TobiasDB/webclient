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
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from urllib.parse import urlparse

from web.fetch import Request
from web.parse import Document
from web.resolve import Resolver

#: whether to FOLLOW ``link`` found on ``doc`` -- the crawl's traversal scope.
Follow = Callable[[Document, str], bool]
#: whether a resolved ``doc`` is a RESULT to yield (vs only traversed for its links).
Collect = Callable[[Document], bool]


def same_origin(doc: Document, link: str) -> bool:
    """Default scope: stay on the document's host."""
    return urlparse(link).hostname == urlparse(doc.url).hostname


@dataclass
class Goal:
    """What to crawl. ``start`` is the entry point(s); ``scope`` decides which links to follow;
    ``collect`` (optional) decides which resolved documents are RESULTS (default: all of them);
    ``max_pages`` bounds how many pages are fetched. Seeds/frontier are derived from this."""

    start: "str | list[str]"
    scope: Follow = same_origin
    collect: "Collect | None" = None
    max_pages: int = 50


class Crawler:
    """Breadth-first crawl over a :class:`~web.resolve.Resolver`, driven by a :class:`Goal`."""

    def __init__(self, resolver: Resolver) -> None:
        self._resolver = resolver

    async def crawl(self, goal: Goal) -> AsyncIterator[Document]:
        """Resolve the goal's entry point(s) and their in-scope links breadth-first, yielding each
        RESULT document once, until the frontier drains or ``max_pages`` pages are fetched."""
        seeds = [goal.start] if isinstance(goal.start, str) else list(goal.start)
        seen: set[str] = set()
        frontier: deque[str] = deque()
        for s in seeds:  # the frontier is internal -- derived from the goal
            if s not in seen:
                seen.add(s)
                frontier.append(s)
        fetched = 0
        while frontier and fetched < goal.max_pages:
            doc = await self._resolver.resolve(Request(url=frontier.popleft()))
            fetched += 1
            if goal.collect is None or goal.collect(doc):
                yield doc
            if doc.kind in ("html", "xml"):  # traverse links even from non-results
                for link in doc.links():
                    if link not in seen and goal.scope(doc, link):
                        seen.add(link)
                        frontier.append(link)

    async def aclose(self) -> None:
        await self._resolver.aclose()


__all__ = ["Crawler", "Goal", "Follow", "Collect", "same_origin"]
