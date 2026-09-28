"""Pagination as middleware -- one more policy in the resolve chain, beside retry and escalate.

A pagination middleware resolves the whole page sequence and returns ONE merged Document, so it
honours the ``Request -> Document`` contract: ``resolver.resolve(req)`` gives you the full
dataset. Each inner page fetch goes through ``next`` (the chain below), so retry / escalate /
rate-limit still apply per page.

"The next page" comes from three places, so there are three middleware implementations that
share the same shape (loop, collect, :func:`_merge`):
  * :func:`paginate_links`  -- follow the ``rel=next`` link (extracted from the DOM),
  * :func:`paginate_param`  -- increment a query parameter (computed from the URL),
  * :func:`paginate_clicks` -- click a Load-more / Next control on a live page (no new URL).

The stop condition is a plain, composable ``until`` (see :mod:`.stops`) -- an empty page is only
one of many (item budget, id/date cutoff, loop guard, last-page marker), usually combined.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from web.fetch import BrowserFetcher, Request
from web.parse import Document, parse, parse_bytes

from .base import Handler, Middleware

#: stop the unfold after a page when this holds (see :mod:`.stops`).
Until = Callable[[Document], bool]


def _merge(pages: list[Document]) -> Document:
    """Combine resolved pages into one Document (concatenated markup), so a downstream
    ``select_all`` spans every page. A single page is returned unchanged."""
    if len(pages) == 1:
        return pages[0]
    first = pages[0]
    body = b"".join(p.content for p in pages)
    return parse_bytes(body, content_type="text/html", url=first.url, status=first.status, error=first.error)


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

    async def mw(request: Request, nxt: Handler) -> Document:
        doc = await nxt(request)
        pages, seen = [doc], {request.url}
        for _ in range(max_pages - 1):
            nxt_url = _next_link(doc)
            if not doc.ok or nxt_url is None or nxt_url in seen or (until and until(doc)):
                break
            seen.add(nxt_url)
            doc = await nxt(request.model_copy(update={"url": nxt_url}))
            pages.append(doc)
        return _merge(pages)

    return mw


def paginate_param(name: str = "page", *, step: int = 1, until: "Until | None" = None, max_pages: int = 20) -> Middleware:
    """PARAM strategy: increment a query parameter (``?page=2``). No next link, so ``until``
    (or ``max_pages``) decides where to stop -- an out-of-range page returns no items, not an error."""

    async def mw(request: Request, nxt: Handler) -> Document:
        doc, url = await nxt(request), request.url
        pages = [doc]
        for _ in range(max_pages - 1):
            if not doc.ok or (until and until(doc)):
                break
            url = _bump(url, name, step)
            doc = await nxt(request.model_copy(update={"url": url}))
            pages.append(doc)
        return _merge(pages)

    return mw


def paginate_clicks(
    browser: BrowserFetcher, more: str, *,
    until: "Until | None" = None, settle: float = 0.3, max_clicks: int = 20,
) -> Middleware:
    """CLICK strategy: drive ONE live page, clicking the ``more`` control (Load-more / Next) and
    re-rendering in place until it is gone, then return the final accumulated Document. There is
    no new URL, so this middleware handles the request via the browser and ignores ``next``."""

    async def mw(request: Request, nxt: Handler) -> Document:
        page = await browser.open(request)
        try:
            for _ in range(max_clicks):
                doc = parse(await page.snapshot())
                if (until and until(doc)) or doc.select(more) is None:
                    break
                await page.click(more)
                await asyncio.sleep(settle)
            return parse(await page.snapshot())
        finally:
            await page.close()

    return mw


__all__ = ["paginate_links", "paginate_param", "paginate_clicks", "Until"]
