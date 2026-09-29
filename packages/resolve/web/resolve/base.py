"""The Resolver: ``Request -> Document`` with the middleware ordering baked in, plus Profiles.

fetch owns the GENERIC framework (``stack`` + the ``Middleware`` type) and the backends; this
Resolver is the OPINIONATED face -- named slots assembled in the one correct order, so a consumer
cannot confuse it. The canonical order (outermost -> innermost) is::

    (custom middleware) -> paginate -> escalate -> retry -> rate -> rotate -> base tier

Every slot is a declarative :class:`~web.resolve.policy.Policy` (``retry=RetryPolicy(...)``,
``rate=RatePolicy(...)``, ``paginate=PaginatePolicy(...)``, ``rotate=RotationPolicy(...)``) that
BUILDS its middleware, so config is clean and a :class:`Profile` (a bundle of them) stays fully
serialisable. The transport ladder is itself a policy: ``escalation=EscalationPolicy(tiers=[fetch
identities], on=[...])`` -- its first tier is the base, the rest are climbed on a block. A raw
``ladder=`` (Fetchers) is the low-level escape (used by ``session()`` / tests); a ready
``Middleware`` may be passed to any slot. A per-slot kwarg overrides the profile.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, TypeAlias, runtime_checkable

from web.fetch import ClientPool, Fetcher, Middleware, Request, WebException, default_pool, stack
from web.fetch import Profile as FetchProfile
from web.parse import Document
from .document import document
from .middleware import escalate as _escalate
from .policy import EscalationPolicy, Policy

#: a resolve slot: a declarative :class:`~web.resolve.policy.Policy` (builds its middleware), or a
#: ready ``Middleware`` (the escape hatch -- not serialisable).
Slot: TypeAlias = Policy | Middleware

#: a raw transport ladder tier (the low-level escape used by ``session()`` + tests): a ready
#: Fetcher, or a fetch :class:`Profile` leased from the pool. The serialisable way is an
#: :class:`~web.resolve.policy.EscalationPolicy`.
Tier: TypeAlias = Fetcher | FetchProfile


def _slot(slot: "Slot | None", pool: ClientPool) -> "Middleware | None":
    """A slot's middleware: a :class:`Policy` builds one from the pool; a ready ``Middleware`` is
    used as-is; ``None`` is nothing."""
    if slot is None:
        return None
    return slot.build(pool) if isinstance(slot, Policy) else slot


class _Keep:
    """The 'unchanged' sentinel for :meth:`Profile.with_` (so a slot can be cleared to ``None``)."""


_KEEP = _Keep()


@dataclass(frozen=True)
class Profile:
    """A reusable, named POLICY bundle -- each slot a declarative :class:`~web.resolve.policy.Policy`
    (which builds its own middleware), so a Profile is self-contained and serialisable (no baked-in
    closures). ``escalation`` is the transport ladder (fetch identities to climb + when); ``retry``
    / ``rate`` / ``paginate`` / ``rotate`` are the wrapping policies. Applied via
    ``Resolver(profile=...)`` / ``resolve(profile=...)``; combine with ``.with_(...)``."""

    escalation: "EscalationPolicy | None" = None
    retry: "Slot | None" = None
    rate: "Slot | None" = None
    paginate: "Slot | None" = None
    rotate: "Slot | None" = None
    middleware: "tuple[Middleware, ...]" = ()
    #: on a TRANSPORT failure (no response, after the middleware chain -- retry/escalate -- has run),
    #: raise a structured WebException by default; set False for a policy that returns the not-ok
    #: (empty) Document instead. An HTTP status (404/500) is a valid response, never a transport
    #: failure, so it is never raised here.
    raise_on_error: bool = True

    def with_(self, *, escalation: "EscalationPolicy | None | _Keep" = _KEEP,
              retry: "Slot | None | _Keep" = _KEEP, rate: "Slot | None | _Keep" = _KEEP,
              paginate: "Slot | None | _Keep" = _KEEP, rotate: "Slot | None | _Keep" = _KEEP,
              middleware: "tuple[Middleware, ...] | _Keep" = _KEEP,
              raise_on_error: "bool | _Keep" = _KEEP) -> "Profile":
        """A copy with some slots overridden (the rest inherited) -- combine or adjust a base profile."""
        return Profile(
            escalation=self.escalation if isinstance(escalation, _Keep) else escalation,
            retry=self.retry if isinstance(retry, _Keep) else retry,
            rate=self.rate if isinstance(rate, _Keep) else rate,
            paginate=self.paginate if isinstance(paginate, _Keep) else paginate,
            rotate=self.rotate if isinstance(rotate, _Keep) else rotate,
            middleware=self.middleware if isinstance(middleware, _Keep) else middleware,
            raise_on_error=self.raise_on_error if isinstance(raise_on_error, _Keep) else raise_on_error,
        )


_EMPTY = Profile()




@runtime_checkable
class _Openable(Protocol):
    """A backend that can open a persistent session (a Session is itself Fetcher-shaped)."""

    async def session(self) -> Fetcher: ...


async def _open_session(tier: Fetcher) -> Fetcher:
    """Open a persistent session on a tier that supports one; a backend without ``session()``
    (e.g. a replay backend) is used as-is."""
    return await tier.session() if isinstance(tier, _Openable) else tier


class Resolver:
    """``Request -> Document``: the base tier wrapped in an ordered middleware chain, then parse.
    The four policy slots are always ordered correctly regardless of kwarg order; a ``profile``
    supplies defaults and a per-slot kwarg overrides it."""

    def __init__(
        self,
        *,
        escalation: "EscalationPolicy | None" = None,
        ladder: "tuple[Tier, ...] | None" = None,  # raw escape (session/tests); prefer escalation
        profile: "Profile | None" = None,
        retry: "Slot | None" = None,
        rate: "Slot | None" = None,
        paginate: "Slot | None" = None,
        rotate: "Slot | None" = None,
        middleware: tuple[Middleware, ...] = (),
        raise_on_error: "bool | None" = None,
        pool: "ClientPool | None" = None,
        _own: bool = False,
    ) -> None:
        p = profile or _EMPTY
        self._pool = pool or default_pool()
        self._raise = raise_on_error if raise_on_error is not None else p.raise_on_error
        esc = escalation if escalation is not None else p.escalation
        # transport tiers: an escalation POLICY (its fetch identities) wins; else a raw ladder
        # (session/tests); else the default HTTP identity. Each tier is leased from the pool (a fetch
        # Profile -> its shared backend; a ready Fetcher/session passes through).
        if esc is not None and esc.tiers:
            self._tiers: tuple[Fetcher, ...] = tuple(self._pool.lease(t) for t in esc.tiers)
            self._esc: "Middleware | None" = esc.build(self._pool)  # escalate over the climb tiers, with its `on`
        elif ladder:
            self._tiers = tuple(self._pool.lease(t) if isinstance(t, FetchProfile) else t for t in ladder)
            self._esc = _escalate(list(self._tiers[1:])) if len(self._tiers) > 1 else None  # default `on`
        else:
            self._tiers = (self._pool.lease(FetchProfile(fingerprint=True)),)
            self._esc = None
        #: whether THIS resolver owns its tiers' lifetime (a session() resolver owns the sessions it
        #: opened; a base resolver's tiers are pool-owned or caller-owned -> it closes nothing).
        self._owned = _own
        # keep the resolved slots so session() can rebuild the same chain over persistent tiers
        self._rt = retry if retry is not None else p.retry
        self._rl = rate if rate is not None else p.rate
        self._pg = paginate if paginate is not None else p.paginate
        self._rot = rotate if rotate is not None else p.rotate
        self._mw = middleware or p.middleware
        self._fetcher = self._stack_over(self._tiers)

    def _stack_over(self, tiers: tuple[Fetcher, ...]) -> Fetcher:
        """Build the ordered middleware chain around the base tier. Each slot is a Policy (builds its
        middleware from the pool) or a ready middleware; escalation is already built (over the climb
        tiers). Order (outermost -> innermost): custom, paginate, escalate, retry, rate, rotate."""
        base_tier = next(iter(tiers))  # non-empty by construction
        chain = tuple(
            m for m in (
                *self._mw,                    # custom, outermost
                _slot(self._pg, self._pool),  # paginate: drives the page loop
                self._esc,                    # escalate: climb the transport ladder on a signal
                _slot(self._rt, self._pool),  # retry: same request on transient failure
                _slot(self._rl, self._pool),  # rate: host politeness
                _slot(self._rot, self._pool), # rotate: fresh identity, innermost (owns the fetch)
            )
            if m is not None
        )
        return stack(base_tier, chain)

    @property
    def pool(self) -> ClientPool:
        """The client pool this resolver leases its backends from (shared with a WebClient's, so a
        per-step resolve policy can reuse it -- one browser, not a relaunch)."""
        return self._pool

    async def resolve(self, request: "Request | str") -> Document:
        """``Request -> Document`` (a bare URL string is a shorthand ``Request``). The middleware
        chain (retry / escalate / rate-limit / paginate) runs first; then, on a TRANSPORT failure
        (no response), this raises a structured :class:`~web.fetch.WebException` by default -- the
        resolve POLICY's choice (``raise_on_error=False`` returns the not-ok empty Document instead).
        An HTTP status (404/500) is a valid response and is never raised."""
        req = Request(url=request) if isinstance(request, str) else request
        snap = await self._fetcher.fetch(req)  # runs the whole middleware chain (retry, escalate, ...)
        if snap.error is not None and self._raise:
            raise WebException(snap.error)
        return document(snap)

    async def __aenter__(self) -> "Resolver":
        """Enter a resolver scope -- ``async with Resolver(...) as rs:`` (closes on exit)."""
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    async def session(self) -> "Resolver":
        """A stateful resolver: open a persistent SESSION on each tier (a cookie jar / browser
        context that survives across resolves) and rebuild the same chain over them. Returns a
        Resolver over the sessions, so a Crawler uses it unchanged -- an authenticated crawl keeps
        its state across pages. Closing it closes the sessions it opened."""
        sessions = tuple([await _open_session(t) for t in self._tiers])
        return Resolver(
            ladder=sessions, retry=self._rt, rate=self._rl, paginate=self._pg,
            middleware=self._mw, raise_on_error=self._raise, pool=self._pool,
            _own=True,  # the session resolver OWNS its sessions (rotation off: a session is ONE identity)
        )

    async def aclose(self) -> None:
        """Close only the tiers this resolver OWNS -- the sessions a ``session()`` resolver opened.
        A base resolver's tiers are leased from the pool (or handed in by a caller), so it closes
        nothing here; the pool's ``aclose`` shuts those (and any launched browser)."""
        if self._owned:
            for tier in self._tiers:
                await tier.aclose()


__all__ = ["Resolver", "Profile"]
