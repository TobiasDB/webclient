"""The resolve interface: a :class:`Resolver` turns a :class:`~web.fetch.Request` into a
:class:`~web.parse.Document` by fetching a Snapshot and parsing it, wrapped in a chain of
**middleware** -- the cross-cutting policies (retry, rate limiting, and later browser
escalation). Middleware is an onion: each is ``async (request, next) -> Document`` and may
short-circuit, retry, or alter the request before calling ``next``.

The base handler is just ``fetch -> parse``; the Resolver composes the middleware around it.
This layer owns the orchestration and the policies; it does not know httpx or lxml -- it uses
the fetch and parse layers through their interfaces.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from web.fetch import Fetcher, Request
from web.parse import Document, parse

#: the innermost/next step in the chain -- a plain request -> Document.
Handler = Callable[[Request], Awaitable[Document]]
#: one policy layer: it receives the request and the next handler and returns a Document.
Middleware = Callable[[Request, Handler], Awaitable[Document]]


def _bind(mw: Middleware, nxt: Handler) -> Handler:
    async def handler(request: Request) -> Document:
        return await mw(request, nxt)

    return handler


class Resolver:
    """``Request -> Document`` over a :class:`~web.fetch.Fetcher` + parse, through a middleware
    chain. ``middleware`` is applied outermost-first (the first entry wraps all the rest)."""

    def __init__(self, fetcher: Fetcher, *, middleware: "tuple[Middleware, ...]" = ()) -> None:
        self._fetcher = fetcher
        self._middleware = middleware

    async def _base(self, request: Request) -> Document:
        """fetch -> parse: the innermost handler the middleware wraps."""
        return parse(await self._fetcher.fetch(request))

    async def resolve(self, request: Request) -> Document:
        handler: Handler = self._base
        for mw in reversed(self._middleware):  # build the onion inside-out
            handler = _bind(mw, handler)
        return await handler(request)

    async def aclose(self) -> None:
        await self._fetcher.aclose()


__all__ = ["Resolver", "Handler", "Middleware"]
