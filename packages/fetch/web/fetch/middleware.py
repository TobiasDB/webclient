"""The middleware FRAMEWORK -- fetch owns it, because middleware wraps the request/transport.

This is only the mechanism: the :data:`Middleware` type and :func:`stack`, the composition. The
IMPLEMENTATIONS (retry, rate_limit, escalate, pagination, and vendor-specific policies) live in a
higher layer (web.resolve), where they compose into per-vendor profiles.

A :data:`Middleware` is ``async (request, next) -> Snapshot``: an onion layer around a fetch. It
may retry the SAME request, re-issue it on a DIFFERENT transport, or modify the request/response
and loop. :func:`stack` wraps a base :class:`~web.fetch.base.Fetcher` in a chain (outermost first)
and is itself a Fetcher, so a middleware-wrapped transport is still just a Fetcher to everything
above.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from .base import Fetcher
from .request import Request
from .snapshot import Snapshot

#: the next step in the chain: a plain request -> Snapshot.
Handler = Callable[[Request], Awaitable[Snapshot]]
#: one onion layer: it receives the request and the next handler and returns a Snapshot.
Middleware = Callable[[Request, Handler], Awaitable[Snapshot]]


def _bind(mw: Middleware, nxt: Handler) -> Handler:
    async def handler(request: Request) -> Snapshot:
        return await mw(request, nxt)

    return handler


class _Stacked:
    def __init__(self, base: Fetcher, middleware: tuple[Middleware, ...]) -> None:
        self._base = base
        self._middleware = middleware

    async def fetch(self, request: Request) -> Snapshot:
        handler: Handler = self._base.fetch
        for mw in reversed(self._middleware):  # build the onion inside-out
            handler = _bind(mw, handler)
        return await handler(request)

    async def aclose(self) -> None:
        await self._base.aclose()


def stack(base: Fetcher, middleware: tuple[Middleware, ...] = ()) -> Fetcher:
    """Wrap ``base`` in a middleware chain (outermost first); the result is itself a Fetcher."""
    return _Stacked(base, middleware)


__all__ = ["Middleware", "Handler", "stack"]
