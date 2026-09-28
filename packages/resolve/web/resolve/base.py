"""The Resolver: ``Request -> Document`` = a middleware-wrapped transport, then parse.

The middleware FRAMEWORK lives in fetch (``web.fetch.stack`` + the ``Middleware`` type); this
layer owns the IMPLEMENTATIONS (:mod:`.middleware`, :mod:`.paginate`) and composes them. A
Resolver stacks a middleware chain around a base Fetcher -- so retry / escalate / paginate all
run at the transport level, producing a Snapshot -- then parses that Snapshot ONCE into a
Document. The chain is a per-vendor "profile": the ordered middlewares that make a given site
behave (its politeness, its escalation, its pagination).
"""

from __future__ import annotations

from web.fetch import Fetcher, Middleware, Request, stack
from web.parse import Document, parse


class Resolver:
    """``Request -> Document``: parse the Snapshot produced by ``fetcher`` wrapped in
    ``middleware`` (the vendor profile; applied outermost-first)."""

    def __init__(self, fetcher: Fetcher, *, middleware: tuple[Middleware, ...] = ()) -> None:
        self._fetcher = stack(fetcher, middleware)

    async def resolve(self, request: Request) -> Document:
        return parse(await self._fetcher.fetch(request))

    async def aclose(self) -> None:
        await self._fetcher.aclose()


#: a vendor profile is just its ordered middleware chain.
Profile = tuple[Middleware, ...]


def profile(*middleware: Middleware) -> Profile:
    """Name a middleware chain -- a per-vendor profile (politeness + escalation + pagination +
    any vendor-specific middleware), ready to hand to a :class:`Resolver`."""
    return middleware


__all__ = ["Resolver", "Profile", "profile"]
