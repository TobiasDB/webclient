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

from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any, cast

from web.fetch import Fetcher, HttpFetcher, Middleware, Request, stack
from web.parse import Document
from .document import document

from .middleware import escalate as _escalate
from .middleware import rate_limit as _rate_limit
from .middleware import retry as _retry


def _slot(value: Any, make: "Callable[[Any], Middleware]") -> "Middleware | None":
    """A slot is empty (None), a ready middleware (callable -> used as-is), or config (passed to
    ``make``). config values (int/float) are not callable; middlewares are."""
    if value is None:
        return None
    return cast(Middleware, value) if callable(value) else make(value)


@dataclass(frozen=True)
class Profile:
    """A reusable, named middleware configuration -- combine a vendor's transport ladder +
    politeness + retry + pagination once, then apply via ``Resolver(profile=...)``. Combinable
    with ``.with_(...)``; a per-slot Resolver kwarg overrides it."""

    ladder: "tuple[Fetcher, ...] | None" = None
    rate_limit: "float | Middleware | None" = None
    retry: "int | Middleware | None" = None
    paginate: "Middleware | None" = None
    middleware: tuple[Middleware, ...] = ()

    def with_(self, **overrides: Any) -> "Profile":
        """A copy with some slots overridden -- combine or adjust a base profile."""
        return replace(self, **overrides)


_EMPTY = Profile()


async def _open_session(tier: Fetcher) -> Fetcher:
    """Open a persistent session on a tier that supports one (a Session is itself Fetcher-shaped);
    a backend without ``session()`` (e.g. a replay backend) is used as-is."""
    opener = getattr(tier, "session", None)
    return cast(Fetcher, await opener()) if opener is not None else tier


class Resolver:
    """``Request -> Document``: the base tier wrapped in an ordered middleware chain, then parse.
    The four policy slots are always ordered correctly regardless of kwarg order; a ``profile``
    supplies defaults and a per-slot kwarg overrides it."""

    def __init__(
        self,
        *,
        ladder: "tuple[Fetcher, ...] | None" = None,
        profile: "Profile | None" = None,
        rate_limit: "float | Middleware | None" = None,
        retry: "int | Middleware | None" = None,
        paginate: "Middleware | None" = None,
        middleware: tuple[Middleware, ...] = (),
    ) -> None:
        p = profile or _EMPTY
        chosen = ladder if ladder is not None else p.ladder
        self._tiers: tuple[Fetcher, ...] = tuple(chosen) if chosen else (HttpFetcher(),)  # empty/None -> default
        # keep the resolved slots so session() can rebuild the same chain over persistent tiers
        self._rl = rate_limit if rate_limit is not None else p.rate_limit
        self._rt = retry if retry is not None else p.retry
        self._pg = paginate if paginate is not None else p.paginate
        self._mw = middleware or p.middleware
        self._fetcher = self._stack_over(self._tiers)

    def _stack_over(self, tiers: tuple[Fetcher, ...]) -> Fetcher:
        """Build the ordered middleware chain around ``tiers[0]``, escalating over the rest."""
        base_tier = next(iter(tiers))  # non-empty by construction
        esc = _escalate(list(tiers[1:])) if len(tiers) > 1 else None
        chain = tuple(
            m for m in (
                *self._mw,                       # custom, outermost
                self._pg,                        # drives the page loop
                esc,                             # climb the transport ladder on a signal
                _slot(self._rt, _retry),         # same request on transient failure
                _slot(self._rl, _rate_limit),    # host politeness, innermost
            )
            if m is not None
        )
        return stack(base_tier, chain)

    async def resolve(self, request: Request) -> Document:
        return document(await self._fetcher.fetch(request))

    async def session(self) -> "Resolver":
        """A stateful resolver: open a persistent SESSION on each tier (a cookie jar / browser
        context that survives across resolves) and rebuild the same chain over them. Returns a
        Resolver over the sessions, so a Crawler uses it unchanged -- an authenticated crawl keeps
        its state across pages. Closing it closes the sessions it opened."""
        sessions = tuple([await _open_session(t) for t in self._tiers])
        return Resolver(
            ladder=sessions, rate_limit=self._rl, retry=self._rt, paginate=self._pg, middleware=self._mw,
        )

    async def aclose(self) -> None:
        for tier in self._tiers:  # close every tier (unused browser tiers are a no-op)
            await tier.aclose()


__all__ = ["Resolver", "Profile"]
