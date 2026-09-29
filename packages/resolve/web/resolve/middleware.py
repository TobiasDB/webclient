"""The middleware implementations -- retry, rate_limit, escalate -- over fetch's framework.

Each is a ``web.fetch.Middleware`` (``async (request, next) -> Snapshot``). retry and rate_limit
are pure transport policies; escalate inspects PARSED content (that is why it lives here, above
parse, not in fetch). Compose them into a per-vendor profile and hand it to a Resolver.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Callable, Sequence
from urllib.parse import urlparse

from web.fetch import ClientPool, Fetcher, Fingerprint, Handler, Middleware, Profile, Request, Snapshot
from web.fetch import emit, fleet as _default_fleet

from .document import document
from .models import ResolveEvent
from .signals import anti_bot, spa

_RETRIABLE_STATUS = frozenset({429, 500, 502, 503, 504})
#: TRANSIENT transport errors worth retrying the same request for. NOT here: tls (cert),
#: url (malformed), redirects (loop), protocol -- those are persistent, a retry can't help.
_RETRIABLE_ERRORS = frozenset({"fetch.timeout", "fetch.connect", "fetch.dns", "fetch.proxy",
                               "fetch.aborted", "fetch.transport"})


def _retriable(snap: Snapshot) -> bool:
    if snap.error is not None:
        return snap.error.code in _RETRIABLE_ERRORS
    return snap.status in _RETRIABLE_STATUS


def retry(max_attempts: int = 3, backoff: float = 0.2) -> Middleware:
    """Retry the SAME request while the Snapshot is retriable (transport error / 429 / 5xx),
    up to ``max_attempts`` with exponential backoff. Returns the last Snapshot either way."""

    async def mw(request: Request, nxt: Handler) -> Snapshot:
        snap = await nxt(request)
        attempt = 1
        while attempt < max_attempts and _retriable(snap):
            emit(ResolveEvent(phase="retry", url=request.url, detail={"attempt": attempt}))
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


def _blocked(snap: Snapshot) -> bool:
    """The default 'this tier was insufficient, climb' rule: a bad TRANSPORT status (a block), or
    a content signal -- an anti-bot wall or a JS-gated shell (a static tier sees an empty page)."""
    if not snap.ok:  # transport-level block (401/403/429/5xx)
        return True
    doc = document(snap)
    return spa(doc) is not None or anti_bot(doc) is not None


def escalate(tiers: "Sequence[Fetcher]", *, blocked: "Callable[[Snapshot], bool] | None" = None) -> Middleware:
    """Walk the escalation LADDER: after the base fetch (via ``next``), if the result looks
    blocked/insufficient, re-issue the SAME request on the next tier, and so on until one succeeds
    or the ladder is exhausted. ``tiers`` are the tiers ABOVE the base; each tier just fetches --
    choosing/ordering them is this policy. (A tier's fetch is a different transport, so it does not
    re-enter retry/rate_limit; wrap a tier with those if it needs them.)"""
    check = blocked or _blocked

    async def mw(request: Request, nxt: Handler) -> Snapshot:
        snap = await nxt(request)
        for i, tier in enumerate(tiers):
            if not check(snap):
                break
            emit(ResolveEvent(phase="escalate", url=request.url, detail={"tier": i + 1}))
            snap = await tier.fetch(request)
        return snap

    return mw


def rotate(pool: ClientPool, fleet: "tuple[Fingerprint, ...] | None" = None) -> Middleware:
    """Present a fresh identity per request: lease a differently-fingerprinted backend from ``pool``
    (a browserforge ``fleet``) and fetch through IT, so repeated requests do not all look identical.
    The fingerprint-rotation POLICY -- unlike retry (which re-issues on the same backend), it
    RE-LEASES a backend per request. ``next`` (the base tier) is unused: rotation owns the fetch,
    and the pool keeps one backend per identity so nothing is relaunched."""
    members = fleet or _default_fleet()

    async def mw(request: Request, nxt: Handler) -> Snapshot:
        backend = pool.lease(Profile(fingerprint=random.choice(members)))
        emit(ResolveEvent(phase="rotate", url=request.url))
        return await backend.fetch(request)

    return mw


__all__ = ["retry", "rate_limit", "escalate", "rotate"]
