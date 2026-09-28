"""Pagination: ``start -> Documents``, in its three real forms -- all plain code, no DSL.

Two mechanisms, because "the next page" comes from one of three places:

  * a LINK (``rel=next``)               -> :func:`paginate` with :func:`next_link`
  * a PARAM/cursor (``?page=2``)        -> :func:`paginate` with :func:`by_param`
  * a CLICK (Load-more / JS Next)       -> :func:`paginate_live` (a live browser page)

Link and param pagination are the SAME unfold over a :class:`~web.resolve.Resolver` -- each
page is a distinct request, so they differ only in the ``next_url`` strategy (extracted vs
computed) and an ``until`` stop (a computed page has no next link to run out of, so it stops
when the page goes empty). Click pagination has NO new URL: it drives one live page, clicks a
control, and re-snapshots in place -- an unfold over a :class:`~web.fetch.LivePage`, not URLs.
The DSL later adds a lazy ``.paginate()`` face over these; the logic lives here.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from web.fetch import BrowserFetcher, Request
from web.parse import Document, parse
from web.resolve import Resolver

#: next-page URL from (document, current URL) -- extracted from the DOM or computed from the URL.
NextUrl = Callable[[Document, str], "str | None"]
#: stop the unfold when this is true of a page (e.g. it has no items left).
Until = Callable[[Document], bool]


def next_link(doc: Document, url: str = "") -> "str | None":
    """LINK strategy: the ``rel=next`` link (or an explicit Next control), resolved absolute."""
    el = doc.select("a[rel~=next], link[rel=next], a[aria-label=Next]")
    return el.attr("href") if el is not None else None


def by_param(name: str = "page", *, step: int = 1) -> NextUrl:
    """PARAM strategy: compute the next URL by incrementing a query parameter (``?page=2``,
    ``?offset=20``). There is no next LINK, so the page number lives in the request -- pair this
    with an ``until`` predicate (or ``max_pages``) to stop, since it always computes a next URL."""

    def strat(doc: Document, url: str) -> "str | None":
        parts = urlsplit(url)
        q = dict(parse_qsl(parts.query))
        try:
            cur = int(q.get(name, "1"))
        except ValueError:
            cur = 1
        q[name] = str(cur + step)
        return urlunsplit(parts._replace(query=urlencode(q)))

    return strat


async def paginate(
    resolver: Resolver,
    start: str,
    *,
    next_url: NextUrl = next_link,
    until: "Until | None" = None,
    max_pages: int = 20,
) -> AsyncIterator[Document]:
    """URL pagination (link OR param): yield each page's Document, following ``next_url`` from
    one to the next -- bounded, deduped, and stopping on a not-ok page or when ``until`` holds."""
    url: "str | None" = start
    seen: set[str] = set()
    for _ in range(max_pages):
        if url is None or url in seen:
            break
        seen.add(url)
        doc = await resolver.resolve(Request(url=url))
        yield doc
        if not doc.ok or (until is not None and until(doc)):
            break
        url = next_url(doc, url)


async def paginate_live(
    browser: BrowserFetcher,
    start: str,
    *,
    more: str,
    until: "Until | None" = None,
    settle: float = 0.3,
    max_pages: int = 20,
) -> AsyncIterator[Document]:
    """CLICK pagination: drive ONE live page, clicking the ``more`` control (Load-more / Next)
    and re-snapshotting, until the control is gone, ``until`` holds, or ``max_pages``. Yields the
    parsed Document at each step (for Load-more the content accumulates; for a JS Next it
    replaces). No new URL is involved -- this is the interaction-gated case a URL unfold cannot do."""
    page = await browser.open(Request(url=start))
    try:
        for _ in range(max_pages):
            doc = parse(await page.snapshot())
            yield doc
            if (until is not None and until(doc)) or doc.select(more) is None:
                break  # no control present -> nothing more to load
            await page.click(more)
            await asyncio.sleep(settle)  # let the new content render
    finally:
        await page.close()


__all__ = ["paginate", "paginate_live", "next_link", "by_param", "NextUrl", "Until"]
