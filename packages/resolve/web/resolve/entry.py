"""The functional entry: ``resolve(url)`` -- one-shot Document OR a resolve session, from ONE call.

The clean face over :class:`Resolver` -- no object to construct, no ``try/finally``, pagination as a
plain kwarg (not a middleware you assemble by hand):

    doc = await resolve(url)                          # one-shot Document
    doc = await resolve(url, paginate="page", max_pages=3)   # merged multi-page dataset
    async with resolve(url, profile=browser) as s:    # a persistent session over a LIVE fetch page
        doc  = await s.doc()                          # the current page as a Document (middleware+parse)
        await s.click(".load-more")                   # interaction delegates to the live fetch page
        more = await s.resolve(other_url)             # same live session, re-resolved

The session composes OVER the fetch layer's live :class:`~web.fetch.Session`: ``s.doc()`` is to a
resolve session what ``session.snapshot()`` is to a fetch session (parse the current page), and the
interactions (``goto``/``click``/``scroll``/``type``/``wait_for``) delegate straight to the live
browser page -- so "resolve then interact then re-resolve" is coherent in ONE session. Interaction
needs a browser-base profile; an HTTP-only session has no live page (``.doc()`` resolves the entry
URL instead, and interaction raises). Reuses the fetch :class:`~web.fetch.Entry` (dual awaitable /
async-context-manager), so the one-shot/session pattern is identical at both layers.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from web.fetch import ClientPool, Entry, Fetcher, Middleware, Request, Snapshot, WebException, err
from web.parse import Document

from .base import Profile, Resolver, Slot
from .document import document
from .policy import EscalationPolicy


@runtime_checkable
class _Page(Protocol):
    """The live-page surface a resolve session drives when its base transport is a browser: the
    current DOM as a Snapshot, plus the interactions that mutate it. A stateless HTTP session has
    none of this (no 'current page'), so it does not satisfy this Protocol."""

    async def snapshot(self) -> Snapshot: ...
    async def goto(self, request: Request) -> object: ...
    async def click(self, selector: str, *, human: bool = False) -> object: ...
    async def scroll(self, selector: "str | None" = None) -> object: ...
    async def type(self, selector: str, text: str) -> object: ...
    async def wait_for(self, selector: str) -> object: ...


class ResolveSession:
    """A persistent resolve session composed over a LIVE fetch session. ``.doc()`` is the current
    page as a Document (a browser parses its live DOM -- reflecting any interaction; an HTTP session
    resolves the entry URL, since it has no live page). ``.resolve`` reaches more URLs sharing the
    session's state; ``goto``/``click``/``scroll``/``type``/``wait_for`` drive the live page (a
    browser-base profile). Closed by the ``async with`` that opened it."""

    def __init__(self, resolver: Resolver, live: Fetcher, request: Request) -> None:
        self._resolver = resolver
        self._live = live  # the base tier's live fetch session (a browser session == a _Page)
        self._request = request
        self._doc: "Document | None" = None  # memo for the HTTP (no-live-page) case only

    def _page(self) -> _Page:
        """The live page, or a clear error when this session's base transport is not a browser."""
        if isinstance(self._live, _Page):
            return self._live
        raise WebException(err("resolve.no_browser",
                               "interaction needs a browser-base profile (this session is HTTP-only)"))

    async def doc(self) -> Document:
        """The session's CURRENT page as a Document -- the resolve analogue of a fetch session's
        ``snapshot()``. A browser session parses its live DOM (so it reflects any interaction); an
        HTTP session, which has no current page, resolves the entry URL (memoised)."""
        if isinstance(self._live, _Page):
            return document(await self._live.snapshot())
        if self._doc is None:
            self._doc = await self._resolver.resolve(self._request)
        return self._doc

    async def resolve(self, request: "Request | str") -> Document:
        """Resolve another URL within this session (state + live page persist; middleware runs)."""
        return await self._resolver.resolve(request)

    async def goto(self, request: "Request | str") -> "ResolveSession":
        """Navigate the live page to ``request`` (chainable). Then ``doc()`` / interact."""
        await self._page().goto(Request(url=request) if isinstance(request, str) else request)
        return self

    async def click(self, selector: str, *, human: bool = False) -> "ResolveSession":
        """Click ``selector`` on the live page (chainable)."""
        await self._page().click(selector, human=human)
        return self

    async def scroll(self, selector: "str | None" = None) -> "ResolveSession":
        """Scroll ``selector`` into view, or the page to its bottom (chainable)."""
        await self._page().scroll(selector)
        return self

    async def type(self, selector: str, text: str) -> "ResolveSession":
        """Type ``text`` into ``selector`` on the live page (chainable)."""
        await self._page().type(selector, text)
        return self

    async def wait_for(self, selector: str) -> "ResolveSession":
        """Wait for ``selector`` to appear on the live page (chainable)."""
        await self._page().wait_for(selector)
        return self

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
        session_resolver = await resolver.session()
        live = session_resolver.base  # the base tier's live fetch session (a browser == a _Page)
        if isinstance(live, _Page):  # position the live page at the entry URL (mirrors `fetch()`)
            await live.goto(req)
        return ResolveSession(session_resolver, live, req)

    return Entry(one_shot, open_session, resolver.aclose)


__all__ = ["resolve", "ResolveSession"]
