"""Pagination: ``start -> Documents`` by walking the next-page link.

A bounded unfold over the resolve layer -- resolve a page, find its "next" link, resolve that,
repeat -- until there is no next link, a page fails, or ``max_pages`` is reached. Plain async
code: it needs only a :class:`~web.resolve.Resolver` and a pure ``next_url`` strategy, so it
runs in isolation with no DSL. The DSL later adds a lazy ``.paginate()`` face over exactly this.

It is the linear sibling of :class:`~web.crawl.Crawler` (which fans out over all links); both
are "reach over resolve". The ``pagination`` signal in web.resolve DETECTS that a page is
paginated; ``next_link`` here EXTRACTS where the next page is -- same selectors, different jobs.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable

from web.fetch import Request
from web.parse import Document
from web.resolve import Resolver

#: extract the next-page URL from a document, or None when there is no next page.
NextUrl = Callable[[Document], "str | None"]


def next_link(doc: Document) -> "str | None":
    """Default strategy: the ``rel=next`` link (or an explicit Next control), resolved absolute."""
    el = doc.select("a[rel~=next], link[rel=next], a[aria-label=Next]")
    return el.attr("href") if el is not None else None


async def paginate(
    resolver: Resolver,
    start: str,
    *,
    next_url: NextUrl = next_link,
    max_pages: int = 20,
) -> AsyncIterator[Document]:
    """Yield each page's Document, following ``next_url`` from one to the next, bounded by
    ``max_pages`` and de-duplicated. Stops on a page that is not ``ok`` or has no next link."""
    url: "str | None" = start
    seen: set[str] = set()
    for _ in range(max_pages):
        if url is None or url in seen:
            break
        seen.add(url)
        doc = await resolver.resolve(Request(url=url))
        yield doc
        if not doc.ok:
            break
        url = next_url(doc)


__all__ = ["paginate", "next_link", "NextUrl"]
