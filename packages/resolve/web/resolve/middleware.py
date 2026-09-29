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

from web.fetch import (
    ClientPool,
    Fetcher,
    Fingerprint,
    Handler,
    Middleware,
    Profile,
    Request,
    Snapshot,
    emit,
)
from web.fetch import fleet as _default_fleet

from .document import document
from .flags import flags
from .models import ResolveEvent

_RETRIABLE_STATUS = frozenset({429, 500, 502, 503, 504})
#: TRANSIENT transport errors worth retrying the same request for. NOT here: tls (cert),
#: url (malformed), redirects (loop), protocol -- those are persistent, a retry can't help.
_RETRIABLE_ERRORS = frozenset(
    {
        "fetch.timeout",
        "fetch.connect",
        "fetch.dns",
        "fetch.proxy",
        "fetch.aborted",
        "fetch.transport",
    }
)


def _retriable(snap: Snapshot) -> bool:
    if snap.error is not None:
        return snap.error.code in _RETRIABLE_ERRORS
    return snap.status in _RETRIABLE_STATUS


def retry(max_attempts: int = 3, backoff: float = 0.2) -> Middleware:
    """Retry the SAME request while the Snapshot is retriable (transport error / 429 / 5xx),
    up to ``max_attempts`` with exponential backoff. Returns the last Snapshot either way.
    """

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
    Reserves each host's slot then waits outside the lock, so other hosts are unaffected.
    """
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


#: remedies (from the flag layer) that a STRONGER TRANSPORT tier can satisfy -- the escalation
#: ladder climbs on these and stops on everything else. This is the flag -> next-profile decision,
#: and it lives in RESOLVE by design: fetch only runs the profile it is handed and has no concept of
#: flags. Per ANTI-BOT.md §4-§5, a JS/fingerprint verdict is answered by climbing browser REALNESS
#: (escalate:browser / escalate:realness) and an IP/ASN verdict by a residential IP (escalate:proxy);
#: a 429 (retry:backoff), a CAPTCHA (a solver, not a tier), and a 5xx / transport error (retry --
#: transient) are NOT climbs, so they are deliberately absent here.
_CLIMB_REMEDIES = frozenset({"escalate:browser", "escalate:realness", "escalate:proxy"})


def transport_remedy(snap: Snapshot) -> "str | None":
    """The transport-level remedy the escalation ladder should apply next, read from the page's
    FLAGS (resolve's conclusion surface): the highest-confidence flag whose remedy a different
    transport can act on -- climb browser realness (``escalate:browser`` / ``escalate:realness``
    from a JS-gated shell or a JS/PoW challenge), swap to a residential IP (``escalate:proxy`` from
    an IP/ASN deny), or back off (``retry:backoff`` from a 429). ``None`` when the page is clean or
    the remedy is not transport-shaped (a CAPTCHA solver, a login, a consent dismiss)."""
    doc = document(snap)
    for flag in flags(doc, snap):  # highest-confidence first
        if flag.remedy in _CLIMB_REMEDIES:
            # a browser render only helps a JS-GATED page (a `spa` shell the browser will populate),
            # not a merely thin/short one: a small but complete record list reads as `empty` yet a
            # browser adds nothing, so climbing there just launches a browser for no gain. Require the
            # `spa` signal for the browser rung; the realness/proxy rungs have no such caveat.
            if flag.remedy == "escalate:browser" and not any(s.name == "spa" for s in flag.signals):
                continue
            return flag.remedy
        if flag.remedy == "retry:backoff":
            return flag.remedy
    return None


def escalate(
    tiers: "Sequence[Fetcher]", *, blocked: "Callable[[Snapshot], bool] | None" = None
) -> Middleware:
    """Walk the escalation LADDER: after the base fetch (via ``next``), decide from the RESULT which
    stronger tier to try next, re-issue the SAME request there, and so on until one succeeds or the
    ladder is exhausted. ``tiers`` are the tiers ABOVE the base; each tier just fetches -- resolve
    chooses/orders them (fetch is flag-unaware). The climb decision is REASON-AWARE by default
    (:func:`transport_remedy`): it climbs only for a remedy a stronger transport satisfies and stops
    on a rate-limit / CAPTCHA / transient error. Passing an explicit ``blocked`` predicate (e.g. from
    an ``EscalationPolicy(on=...)`` status list) restores the plain climb-while-true behaviour. (A
    tier's fetch is a different transport, so it does not re-enter retry/rate_limit; wrap a tier with
    those if it needs them.)"""

    async def mw(request: Request, nxt: Handler) -> Snapshot:
        snap = await nxt(request)
        for i, tier in enumerate(tiers):
            if blocked is not None:  # explicit predicate (status/error tokens) -- plain climb
                if not blocked(snap):
                    break
                remedy: "str | None" = None
            else:  # default: the flag -> next-profile decision
                remedy = transport_remedy(snap)
                if remedy not in _CLIMB_REMEDIES:
                    break
            emit(
                ResolveEvent(
                    phase="escalate", url=request.url, detail={"tier": i + 1, "remedy": remedy}
                )
            )
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


__all__ = ["retry", "rate_limit", "escalate", "rotate", "transport_remedy"]
