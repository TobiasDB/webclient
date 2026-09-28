"""The Resolver: ``Request -> Document`` with the middleware ordering baked in, plus Profiles.

fetch owns the GENERIC framework (``stack`` + the ``Middleware`` type -- raw and unordered, for
anyone composing their own). This Resolver is the OPINIONATED face: it exposes the known
middlewares as named slots and assembles them in the one correct order, so a consumer cannot
confuse it. The canonical order (outermost -> innermost) is::

    (custom middleware) -> paginate -> escalate -> retry -> rate_limit -> base transport

which is why rate_limit throttles every retry and every page, retry sits inside escalate, and
pagination drives the whole thing. A slot takes either config (``retry=3``, ``rate_limit=0.5``,
``escalate=browser``) or a ready-built middleware (``retry=retry(3, backoff=0)``); ``paginate``
takes a pagination middleware; ``middleware=`` adds custom layers outermost.

A :class:`Profile` bundles these slots into one named, reusable unit -- combine a vendor's
politeness + escalation + retry + pagination once, then ``Resolver(fetcher, profile=ACME)``. A
profile is combinable (``ACME.with_(retry=5)``) and a per-slot kwarg overrides it.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any, cast

from web.fetch import Fetcher, Middleware, Request, stack
from web.parse import Document, parse

from .middleware import escalate as _escalate
from .middleware import rate_limit as _rate_limit
from .middleware import retry as _retry


def _slot(value: Any, make: "Callable[[Any], Middleware]") -> "Middleware | None":
    """A slot is empty (None), a ready middleware (callable -> used as-is), or config (passed to
    ``make``). config values (int/float/Fetcher) are not callable; middlewares are."""
    if value is None:
        return None
    return cast(Middleware, value) if callable(value) else make(value)


@dataclass(frozen=True)
class Profile:
    """A reusable, named middleware configuration -- combine a vendor's politeness + escalation +
    retry + pagination (+ custom) once, then apply to any base fetcher via ``Resolver(fetcher,
    profile=...)``. Slots take config or a ready middleware, exactly like Resolver's kwargs."""

    rate_limit: "float | Middleware | None" = None
    retry: "int | Middleware | None" = None
    escalate: "Fetcher | Middleware | None" = None
    paginate: "Middleware | None" = None
    middleware: tuple[Middleware, ...] = ()

    def with_(self, **overrides: Any) -> "Profile":
        """A copy with some slots overridden -- combine or adjust a base profile."""
        return replace(self, **overrides)


_EMPTY = Profile()


class Resolver:
    """``Request -> Document``: a base Fetcher wrapped in an ordered middleware chain, then parse.
    The four policy slots are always ordered correctly regardless of kwarg order; a ``profile``
    supplies defaults and a per-slot kwarg overrides it; ``middleware`` adds custom layers."""

    def __init__(
        self,
        fetcher: Fetcher,
        *,
        profile: "Profile | None" = None,
        rate_limit: "float | Middleware | None" = None,
        retry: "int | Middleware | None" = None,
        escalate: "Fetcher | Middleware | None" = None,
        paginate: "Middleware | None" = None,
        middleware: tuple[Middleware, ...] = (),
    ) -> None:
        p = profile or _EMPTY
        rl = rate_limit if rate_limit is not None else p.rate_limit
        rt = retry if retry is not None else p.retry
        es = escalate if escalate is not None else p.escalate
        pg = paginate if paginate is not None else p.paginate
        mw = middleware or p.middleware
        chain = tuple(
            m for m in (
                *mw,                             # custom, outermost
                pg,                              # drives the page loop
                _slot(es, _escalate),            # different transport on a signal
                _slot(rt, _retry),               # same request on transient failure
                _slot(rl, _rate_limit),          # host politeness, innermost
            )
            if m is not None
        )
        self._fetcher = stack(fetcher, chain)

    async def resolve(self, request: Request) -> Document:
        return parse(await self._fetcher.fetch(request))

    async def aclose(self) -> None:
        await self._fetcher.aclose()


__all__ = ["Resolver", "Profile"]
