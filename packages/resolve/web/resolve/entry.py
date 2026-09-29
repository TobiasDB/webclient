"""The functional entry: ``resolve(url)`` -- one-shot Document OR a resolve session, from ONE call.

The clean face over :class:`Resolver` -- no object to construct, no ``try/finally``, pagination as a
plain kwarg (not a middleware you assemble by hand):

    doc = await resolve(url)                          # one-shot Document
    doc = await resolve(url, paginate="page", max_pages=3)   # merged multi-page dataset
    async with resolve(url, profile=vendor) as session:      # a persistent session
        home = await session.doc()
        more = await session.resolve(other_url)       # same cookies / connection

Reuses the fetch layer's :class:`~web.fetch.Entry` (the dual awaitable / async-context-manager), so
the one-shot/session pattern is identical at both layers.
"""

from __future__ import annotations

from web.fetch import ClientPool, Entry, Middleware, Request
from web.parse import Document

from .base import Profile, Resolver, Slot
from .policy import EscalationPolicy


class ResolveSession:
    """A persistent resolve session: ``.doc()`` resolves the entry URL (once, memoised), ``.resolve``
    reaches more URLs sharing the session's state (cookies / connections). Closed by the ``async
    with`` that opened it."""

    def __init__(self, resolver: Resolver, request: Request) -> None:
        self._resolver = resolver
        self._request = request
        self._doc: "Document | None" = None

    async def doc(self) -> Document:
        """The entry URL resolved to a Document (memoised for the session)."""
        if self._doc is None:
            self._doc = await self._resolver.resolve(self._request)
        return self._doc

    async def resolve(self, request: "Request | str") -> Document:
        """Resolve another URL within this session (state persists)."""
        return await self._resolver.resolve(request)

    async def aclose(self) -> None:
        await self._resolver.aclose()


def resolve(request: "Request | str", *, profile: "Profile | None" = None,
            escalation: "EscalationPolicy | None" = None, retry: "Slot | None" = None,
            rate: "Slot | None" = None, paginate: "Slot | None" = None, rotate: "Slot | None" = None,
            middleware: "tuple[Middleware, ...]" = (), raise_on_error: bool = True,
            pool: "ClientPool | None" = None) -> "Entry[Document, ResolveSession]":
    """Resolve ``request`` (a URL or a :class:`~web.fetch.Request`) to a Document. ``await`` it for a
    one-shot Document, or ``async with resolve(...) as session:`` for a persistent session. Every
    slot is a declarative :class:`~web.resolve.policy.Policy` -- ``escalation=EscalationPolicy(...)``
    (the transport ladder), ``retry=RetryPolicy(...)``, ``rate=RatePolicy(...)``,
    ``paginate=PaginatePolicy(...)``, ``rotate=RotationPolicy(...)`` -- so the config stays clean and
    serialisable; ``profile`` supplies a bundle and a per-slot kwarg overrides it. The backend is
    LEASED from ``pool`` (or the process default), reused across calls."""
    resolver = Resolver(profile=profile, escalation=escalation, retry=retry, rate=rate,
                        paginate=paginate, rotate=rotate, middleware=middleware,
                        raise_on_error=raise_on_error, pool=pool)
    req = Request(url=request) if isinstance(request, str) else request

    async def one_shot() -> Document:
        try:
            return await resolver.resolve(req)
        finally:
            await resolver.aclose()

    async def open_session() -> ResolveSession:
        return ResolveSession(await resolver.session(), req)

    return Entry(one_shot, open_session, resolver.aclose)


__all__ = ["resolve", "ResolveSession"]
