"""The Resolver: ``Request -> Document`` with the middleware ordering baked in, plus Profiles.

fetch owns the GENERIC framework (``stack`` + the ``Middleware`` type) and the backends; this
Resolver is the OPINIONATED face -- named slots assembled in the one correct order, so a consumer
cannot confuse it. The canonical order (outermost -> innermost) is::

    (custom middleware) -> paginate -> escalate(ladder) -> retry -> rate_limit -> base tier

The transport is a ``ladder`` (a policy, not a fixed fetcher): ``ladder[0]`` is the base tier
(default: plain http) and the rest are escalation tiers climbed on a block signal (see
:func:`~web.resolve.tiers.ladder`). rate_limit throttles every retry and every page; retry sits
inside escalation; pagination drives the whole thing.

Slots take config (``retry=3``, ``rate_limit=0.5``) or a ready middleware; ``paginate`` takes a
pagination middleware; ``middleware=`` adds custom layers outermost. A :class:`Profile` bundles
the slots into a named, reusable per-vendor unit, combinable with ``.with_(...)``; a per-slot
kwarg overrides the profile.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, TypeAlias, runtime_checkable

from web.fetch import ClientPool, Fetcher, Middleware, Request, WebException, default_pool, stack
from web.fetch import Profile as FetchProfile
from web.parse import Document
from .document import document

from .middleware import escalate as _escalate
from .middleware import rate_limit as _rate_limit
from .middleware import retry as _retry

#: a transport ladder tier: a ready :class:`~web.fetch.Fetcher`, or a fetch :class:`Profile`
#: (materialised to its fetcher) -- so a resolve profile's escalation ladder is written as fetch
#: profiles (an HTTP tier, a browser tier, ...).
Tier: TypeAlias = Fetcher | FetchProfile


class _Keep:
    """The 'unchanged' sentinel for :meth:`Profile.with_` (so a slot can be cleared to ``None``)."""


_KEEP = _Keep()


@dataclass(frozen=True)
class Profile:
    """A reusable, named POLICY bundle -- a vendor's transport ladder (fetch profiles/fetchers) +
    politeness + retry + escalation + pagination, combined once and applied via
    ``Resolver(profile=...)`` / ``resolve(profile=...)``. Combinable with ``.with_(...)``; a per-slot
    kwarg overrides it. The ladder holds fetch profiles, so the transport identity (proxy /
    fingerprint / browser per tier) is defined once at the fetch layer and reused here."""

    ladder: "tuple[Tier, ...] | None" = None
    rate_limit: "float | Middleware | None" = None
    retry: "int | Middleware | None" = None
    paginate: "Middleware | None" = None
    middleware: "tuple[Middleware, ...]" = ()
    #: on a TRANSPORT failure (no response, after the middleware chain -- retry/escalate -- has run),
    #: raise a structured WebException by default; set False for a policy that returns the not-ok
    #: (empty) Document instead. An HTTP status (404/500) is a valid response, never a transport
    #: failure, so it is never raised here.
    raise_on_error: bool = True

    def with_(self, *, ladder: "tuple[Tier, ...] | None | _Keep" = _KEEP,
              rate_limit: "float | Middleware | None | _Keep" = _KEEP,
              retry: "int | Middleware | None | _Keep" = _KEEP,
              paginate: "Middleware | None | _Keep" = _KEEP,
              middleware: "tuple[Middleware, ...] | _Keep" = _KEEP,
              raise_on_error: "bool | _Keep" = _KEEP) -> "Profile":
        """A copy with some slots overridden (the rest inherited) -- combine or adjust a base profile."""
        return Profile(
            ladder=self.ladder if isinstance(ladder, _Keep) else ladder,
            rate_limit=self.rate_limit if isinstance(rate_limit, _Keep) else rate_limit,
            retry=self.retry if isinstance(retry, _Keep) else retry,
            paginate=self.paginate if isinstance(paginate, _Keep) else paginate,
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
        ladder: "tuple[Tier, ...] | None" = None,
        profile: "Profile | None" = None,
        rate_limit: "float | Middleware | None" = None,
        retry: "int | Middleware | None" = None,
        paginate: "Middleware | None" = None,
        middleware: tuple[Middleware, ...] = (),
        raise_on_error: "bool | None" = None,
        pool: "ClientPool | None" = None,
        _own: bool = False,
    ) -> None:
        p = profile or _EMPTY
        self._pool = pool or default_pool()
        self._raise = raise_on_error if raise_on_error is not None else p.raise_on_error
        chosen = ladder if ladder is not None else p.ladder
        # LEASE each tier from the pool -- a fetch Profile leases its SHARED backend (browser
        # launched once, reused); a ready Fetcher (a caller's, or an opened session) passes through.
        # Empty/None -> the default HTTP tier, leased from the pool.
        self._tiers: tuple[Fetcher, ...] = (
            tuple(self._pool.lease(t) if isinstance(t, FetchProfile) else t for t in chosen)
            if chosen else (self._pool.lease(FetchProfile()),))
        #: whether THIS resolver owns its tiers' lifetime (a session() resolver owns the sessions it
        #: opened; a base resolver's tiers are pool-owned or caller-owned -> it closes nothing).
        self._owned = _own
        # keep the resolved slots so session() can rebuild the same chain over persistent tiers
        self._rl = rate_limit if rate_limit is not None else p.rate_limit
        self._rt = retry if retry is not None else p.retry
        self._pg = paginate if paginate is not None else p.paginate
        self._mw = middleware or p.middleware
        self._fetcher = self._stack_over(self._tiers)

    def _stack_over(self, tiers: tuple[Fetcher, ...]) -> Fetcher:
        """Build the ordered middleware chain around ``tiers[0]``, escalating over the rest. A slot
        holds config (an int/float built into its middleware) or a ready middleware (used as-is);
        the ``isinstance`` narrows the union cleanly, no cast needed."""
        base_tier = next(iter(tiers))  # non-empty by construction
        esc = _escalate(list(tiers[1:])) if len(tiers) > 1 else None
        retry_mw = _retry(self._rt) if isinstance(self._rt, int) else self._rt
        rate_mw = _rate_limit(self._rl) if isinstance(self._rl, (int, float)) else self._rl
        chain = tuple(
            m for m in (
                *self._mw,          # custom, outermost
                self._pg,           # drives the page loop
                esc,                # climb the transport ladder on a signal
                retry_mw,           # same request on transient failure
                rate_mw,            # host politeness, innermost
            )
            if m is not None
        )
        return stack(base_tier, chain)

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
            ladder=sessions, rate_limit=self._rl, retry=self._rt, paginate=self._pg,
            middleware=self._mw, raise_on_error=self._raise, pool=self._pool,
            _own=True,  # the session resolver OWNS its sessions
        )

    async def aclose(self) -> None:
        """Close only the tiers this resolver OWNS -- the sessions a ``session()`` resolver opened.
        A base resolver's tiers are leased from the pool (or handed in by a caller), so it closes
        nothing here; the pool's ``aclose`` shuts those (and any launched browser)."""
        if self._owned:
            for tier in self._tiers:
                await tier.aclose()


__all__ = ["Resolver", "Profile"]
