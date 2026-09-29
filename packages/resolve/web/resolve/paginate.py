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
import json as _json
from collections.abc import Callable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import JsonValue

from web.fetch import BrowserFetcher, Handler, Middleware, Request, Snapshot
from web.kernel import err
from web.parse import Document
from .document import document

#: stop the unfold after a page when this holds (see :mod:`.stops`).
Until = Callable[[Document], bool]


def _merge(snaps: list[Snapshot]) -> Snapshot:
    """Combine resolved pages into ONE Snapshot whose parsed Document spans every page. Each page's
    ``<body>`` INNER html is concatenated under a single root -- concatenating whole ``<html>``
    documents does NOT work (lxml keeps only the first root, silently dropping later pages). A single
    page is returned unchanged. Events accumulate across pages."""
    if len(snaps) == 1:
        return snaps[0]
    parts: list[str] = []
    for s in snaps:
        body = document(s).select("body")
        parts.append(body.inner_html if body is not None else s.content.decode("utf-8", "replace"))
    combined = ("<!doctype html><html><body>" + "".join(parts) + "</body></html>").encode("utf-8")
    return snaps[0].model_copy(update={
        "content": combined,
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
            doc = document(snap)
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
            if not snap.ok or (until and until(document(snap))):
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
        try:
            session = await browser.session()  # the session owns the page
        except Exception as exc:
            return Snapshot(request=request, url=request.url, error=err("fetch.transport", str(exc)))
        try:
            await session.goto(request)
            for _ in range(max_clicks):
                doc = document(await session.snapshot())
                if (until and until(doc)) or doc.select(more) is None:
                    break
                await session.click(more)
                await asyncio.sleep(settle)
            return await session.snapshot()
        except Exception as exc:  # a nav/interaction failure is data, not a raise
            return Snapshot(request=request, url=request.url, error=err("fetch.transport", str(exc)))
        finally:
            await session.aclose()

    return mw


def paginate_cursor(
    *, cursor_path: str, param: str, items_path: str = "", until: "Until | None" = None, max_pages: int = 50,
) -> Middleware:
    """CURSOR strategy for JSON APIs: read a next-cursor token from the response envelope
    (``cursor_path``, a dotted path) and re-issue with it as the ``param`` query parameter, until
    the cursor is absent/empty. The pages' item lists (``items_path``, default the whole body) are
    concatenated into ONE merged JSON array Snapshot -- so a downstream ``.json()`` sees every item
    across pages. ``until`` stops early on the parsed page."""

    async def mw(request: Request, nxt: Handler) -> Snapshot:
        snap = await nxt(request)
        items: list[JsonValue] = []
        last = snap
        for _ in range(max_pages):
            if not snap.ok:
                break
            doc = document(snap)
            try:
                doc.json()  # validate JSON before navigating; non-JSON ends the unfold
            except (ValueError, _json.JSONDecodeError):
                break
            page_items = doc.at(items_path)  # dotted-path dig (shared with parse -- no fork)
            if isinstance(page_items, list):
                items.extend(page_items)
            elif page_items is not None:
                items.append(page_items)
            cursor = doc.at(cursor_path)
            if not cursor or (until and until(doc)):
                break
            url = _bump_param(request.url, param, str(cursor))
            snap = await nxt(request.model_copy(update={"url": url}))
            last = snap
        merged = _json.dumps(items).encode("utf-8")
        return last.model_copy(update={"content": merged, "url": request.url,
                                       "headers": {**last.headers, "content-type": "application/json"}})

    return mw


def _bump_param(url: str, name: str, value: str) -> str:
    parts = urlsplit(url)
    q = dict(parse_qsl(parts.query))
    q[name] = value
    return urlunsplit(parts._replace(query=urlencode(q)))


__all__ = ["paginate_links", "paginate_param", "paginate_clicks", "paginate_cursor", "Until"]
