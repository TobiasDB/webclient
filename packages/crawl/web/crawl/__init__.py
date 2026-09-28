"""web.crawl -- reach: ``seeds -> Documents``.

A frontier over the resolve layer: resolve a URL to a Document, follow the links it yields
that pass a policy, repeat -- breadth-first, deduplicated, bounded by ``max_pages``. Streams
Documents as an async generator so a caller can consume (and stop) as it goes. It knows only
the resolve interface, not fetch/parse internals.

    from web.fetch import HttpFetcher
    from web.resolve import Resolver
    from web.crawl import Crawler
    async for doc in Crawler(Resolver(HttpFetcher())).crawl(["https://example.com"]):
        ...
"""

from __future__ import annotations

from collections import deque
from collections.abc import AsyncIterator, Callable, Iterable
from urllib.parse import urlparse

from web.fetch import Request
from web.parse import Document
from web.resolve import Resolver

from .paginate import NextUrl, Until, by_param, next_link, paginate, paginate_live

#: whether to follow ``link`` found on ``doc`` -- the crawl's scope policy.
Follow = Callable[[Document, str], bool]


def same_origin(doc: Document, link: str) -> bool:
    """Default scope: stay on the document's host."""
    return urlparse(link).hostname == urlparse(doc.url).hostname


class Crawler:
    """Breadth-first crawl over a :class:`~web.resolve.Resolver`. ``follow`` decides which of a
    page's links to enqueue (default: same origin)."""

    def __init__(self, resolver: Resolver, *, follow: Follow = same_origin) -> None:
        self._resolver = resolver
        self._follow = follow

    async def crawl(
        self, seeds: Iterable[str], *, max_pages: int = 50
    ) -> AsyncIterator[Document]:
        """Resolve the seeds and their in-scope links breadth-first, yielding each Document
        once, until the frontier drains or ``max_pages`` is reached."""
        seen: set[str] = set()
        frontier: deque[str] = deque()
        for s in seeds:
            if s not in seen:
                seen.add(s)
                frontier.append(s)
        count = 0
        while frontier and count < max_pages:
            doc = await self._resolver.resolve(Request(url=frontier.popleft()))
            count += 1
            yield doc
            if doc.ok and doc.kind in ("html", "xml"):
                for link in doc.links():
                    if link not in seen and self._follow(doc, link):
                        seen.add(link)
                        frontier.append(link)

    async def aclose(self) -> None:
        await self._resolver.aclose()


__all__ = ["Crawler", "Follow", "same_origin", "paginate", "paginate_live", "next_link", "by_param", "NextUrl", "Until"]
