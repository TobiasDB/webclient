"""Pagination middleware implementations -- more policies over fetch's framework.

Each is a ``web.fetch.Middleware`` that resolves the whole page sequence and returns ONE merged
Snapshot (the Resolver parses it once). Inner page fetches go through ``next``, so retry / rate_limit
below still apply per page. Three implementations, one per source of "the next page":
  * :func:`paginate_links`  -- follow the ``rel=next`` link (parsed from the page),
  * :func:`paginate_param`  -- increment a query parameter (computed from the URL),
  * :func:`paginate_clicks` -- click a Load-more / Next control on a live page (no new URL).

These are REFERENCE implementations. A consumer writes their own the same way -- a cursor
paginator, an API-envelope paginator -- and stacks it in a profile; the framework does not care.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from web.fetch import BrowserFetcher, Handler, Middleware, Request, Snapshot
from web.parse import Document, parse

#: stop the unfold after a page when this holds (see :mod:`.stops`).
Until = Callable[[Document], bool]


def _merge(snaps: list[Snapshot]) -> Snapshot:
    """Combine resolved pages into one Snapshot (concatenated markup + accumulated events), so a
    downstream parse+select_all spans every page. A single page is returned unchanged."""
    if len(snaps) == 1:
        return snaps[0]
    return snaps[0].model_copy(update={
        "content": b"".join(s.content for s in snaps),
        "events": [e for s in snaps for e in s.events],
    })


def _next_link(doc: Document) -> "str | None":
    el = doc.select("a[rel~=next], link[rel=next], a[aria-label=Next]")
    return el.attr("href") if el is not None else None


def _bump(url: str, name: str, step: int) -> str:
    parts = urlsplit(url)
    q = dict(parse_qsl(parts.query))
    try:
        cur = int(q.get(name, "1"))
    except ValueError:
        cur = 1
    q[name] = str(cur + step)
    return urlunsplit(parts._replace(query=urlencode(q)))


def paginate_links(*, until: "Until | None" = None, max_pages: int = 20) -> Middleware:
    """LINK strategy: follow the ``rel=next`` link from page to page, merging the results."""

    async def mw(request: Request, nxt: Handler) -> Snapshot:
        snap = await nxt(request)
        snaps, seen = [snap], {request.url}
        for _ in range(max_pages - 1):
            doc = parse(snap)
            nxt_url = _next_link(doc)
            if not snap.ok or nxt_url is None or nxt_url in seen or (until and until(doc)):
                break
            seen.add(nxt_url)
            snap = await nxt(request.model_copy(update={"url": nxt_url}))
            snaps.append(snap)
        return _merge(snaps)

    return mw


def paginate_param(name: str = "page", *, step: int = 1, until: "Until | None" = None, max_pages: int = 20) -> Middleware:
    """PARAM strategy: increment a query parameter (``?page=2``). No next link, so ``until`` (or
    ``max_pages``) decides where to stop -- an out-of-range page returns no items, not an error."""

    async def mw(request: Request, nxt: Handler) -> Snapshot:
        snap, url = await nxt(request), request.url
        snaps = [snap]
        for _ in range(max_pages - 1):
            if not snap.ok or (until and until(parse(snap))):
                break
            url = _bump(url, name, step)
            snap = await nxt(request.model_copy(update={"url": url}))
            snaps.append(snap)
        return _merge(snaps)

    return mw


def paginate_clicks(
    browser: BrowserFetcher, more: str, *,
    until: "Until | None" = None, settle: float = 0.3, max_clicks: int = 20,
) -> Middleware:
    """CLICK strategy: drive ONE live page, clicking the ``more`` control until it is gone, then
    return the final accumulated Snapshot. No new URL, so this middleware handles the request via
    the browser and ignores ``next``."""

    async def mw(request: Request, nxt: Handler) -> Snapshot:
        page = await browser.open(request)
        try:
            for _ in range(max_clicks):
                snap = await page.snapshot()
                doc = parse(snap)
                if (until and until(doc)) or doc.select(more) is None:
                    break
                await page.click(more)
                await asyncio.sleep(settle)
            return await page.snapshot()
        finally:
            await page.close()

    return mw


__all__ = ["paginate_links", "paginate_param", "paginate_clicks", "Until"]
