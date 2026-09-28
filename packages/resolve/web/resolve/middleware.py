"""The built-in resolve policies, as middleware. Each is a small factory returning a
:class:`~web.resolve.base.Middleware`. Compose them in a Resolver's chain (outermost first).

Browser escalation (static -> rendered on a JS-gated page) is another middleware, added once
the fetch layer grows a browser Fetcher; the chain is the extension point, so it needs no
change here.
"""

from __future__ import annotations

import asyncio
from urllib.parse import urlparse

from web.fetch import Fetcher, Request
from web.parse import Document, parse

from .base import Handler, Middleware
from .signals import detect

_RETRIABLE_STATUS = frozenset({429, 500, 502, 503, 504})


def _retriable(doc: Document) -> bool:
    """A transport failure or a retriable server status -- worth another attempt."""
    if doc.error is not None and doc.error.code == "fetch.transport":
        return True
    return doc.status in _RETRIABLE_STATUS


def retry(max_attempts: int = 3, backoff: float = 0.2) -> Middleware:
    """Retry the request while the Document is retriable, up to ``max_attempts``, with
    exponential backoff. Returns the last Document either way (a failure is data)."""

    async def mw(request: Request, nxt: Handler) -> Document:
        doc = await nxt(request)
        attempt = 1
        while attempt < max_attempts and _retriable(doc):
            await asyncio.sleep(backoff * (2 ** (attempt - 1)))
            doc = await nxt(request)
            attempt += 1
        return doc

    return mw


def rate_limit(min_interval: float) -> Middleware:
    """Keep at least ``min_interval`` seconds between requests to the same host (politeness).
    Reserves each host's next slot, then waits outside the lock so other hosts are unaffected."""
    last: dict[str, float] = {}
    lock = asyncio.Lock()

    async def mw(request: Request, nxt: Handler) -> Document:
        host = urlparse(request.url).hostname or ""
        loop = asyncio.get_running_loop()
        async with lock:
            earliest = max(loop.time(), last.get(host, 0.0) + min_interval)
            last[host] = earliest
        if (wait := earliest - loop.time()) > 0:
            await asyncio.sleep(wait)
        return await nxt(request)

    return mw


def escalate(browser: Fetcher, *, when: tuple[str, ...] = ("spa",)) -> Middleware:
    """The signals -> policy payoff: after a static resolve, if a render-worthy signal fired
    (``spa`` by default -- the page is a JS-gated shell), re-fetch through ``browser`` (a
    render-capable Fetcher) and re-parse. A page that renders server-side skips the browser, so
    the expensive tier is used only when the evidence says it is needed (the adaptive rule)."""

    async def mw(request: Request, nxt: Handler) -> Document:
        doc = await nxt(request)
        if doc.kind == "html" and {s.name for s in detect(doc)} & set(when):
            return parse(await browser.fetch(request))
        return doc

    return mw


__all__ = ["retry", "rate_limit", "escalate"]
