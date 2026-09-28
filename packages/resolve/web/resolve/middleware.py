"""The middleware implementations -- retry, rate_limit, escalate -- over fetch's framework.

Each is a ``web.fetch.Middleware`` (``async (request, next) -> Snapshot``). retry and rate_limit
are pure transport policies; escalate inspects PARSED content (that is why it lives here, above
parse, not in fetch). Compose them into a per-vendor profile and hand it to a Resolver.
"""

from __future__ import annotations

import asyncio
from urllib.parse import urlparse

from web.fetch import Fetcher, Handler, Middleware, Request, Snapshot
from web.parse import parse

from .signals import detect

_RETRIABLE_STATUS = frozenset({429, 500, 502, 503, 504})


def _retriable(snap: Snapshot) -> bool:
    if snap.error is not None and snap.error.code == "fetch.transport":
        return True
    return snap.status in _RETRIABLE_STATUS


def retry(max_attempts: int = 3, backoff: float = 0.2) -> Middleware:
    """Retry the SAME request while the Snapshot is retriable (transport error / 429 / 5xx),
    up to ``max_attempts`` with exponential backoff. Returns the last Snapshot either way."""

    async def mw(request: Request, nxt: Handler) -> Snapshot:
        snap = await nxt(request)
        attempt = 1
        while attempt < max_attempts and _retriable(snap):
            await asyncio.sleep(backoff * (2 ** (attempt - 1)))
            snap = await nxt(request)
            attempt += 1
        return snap

    return mw


def rate_limit(min_interval: float) -> Middleware:
    """Keep at least ``min_interval`` seconds between requests to the same host (politeness).
    Reserves each host's slot then waits outside the lock, so other hosts are unaffected."""
    last: dict[str, float] = {}
    lock = asyncio.Lock()

    async def mw(request: Request, nxt: Handler) -> Snapshot:
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
    """Re-issue the SAME request on a DIFFERENT transport: after a static fetch, parse the
    Snapshot and, if a render-worthy signal fired (``spa`` -- a JS-gated shell), re-fetch it via
    ``browser``. The expensive tier runs only when the evidence says so (the adaptive rule)."""

    async def mw(request: Request, nxt: Handler) -> Snapshot:
        snap = await nxt(request)
        doc = parse(snap)
        if doc.kind == "html" and {s.name for s in detect(doc)} & set(when):
            return await browser.fetch(request)
        return snap

    return mw


__all__ = ["retry", "rate_limit", "escalate"]
