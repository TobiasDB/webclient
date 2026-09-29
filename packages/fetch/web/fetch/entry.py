"""The functional entry: ``fetch(url)`` -- one-shot OR a session, from ONE call.

Two shapes from one function, no object to construct and no ``try/finally``:

    snap = await fetch(url)                         # one-shot Snapshot
    async with fetch(url, browser=True) as page:    # a live session (owns the page)
        await page.click("#more"); shot = await page.snapshot()

:class:`Entry` is the dual awaitable / async-context-manager this returns (reused by the resolve
layer for ``resolve()``). A :class:`Profile` bundles the transport identity (proxy / fingerprint /
headers / browser) once and is inheritable via ``.with_(...)``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Generator
from dataclasses import dataclass, field
from typing import Generic, Protocol, TypeVar, runtime_checkable

from .browser import BrowserFetcher, BrowserSession
from .fingerprint import Fingerprint
from .http import HttpFetcher
from .proxy import Proxy
from .models import Request, Session, Snapshot

V = TypeVar("V")


@runtime_checkable
class _Closeable(Protocol):
    async def aclose(self) -> None: ...


S = TypeVar("S", bound=_Closeable)


class Entry(Generic[V, S]):
    """Awaitable OR async context manager. ``await entry`` runs the ONE-SHOT and returns its value;
    ``async with entry as s`` opens a SESSION ``s`` and closes it (and any owned transport) on exit.
    The two paths share nothing but the request/config the entry was built from."""

    __slots__ = ("_one_shot", "_open", "_on_exit", "_session")

    def __init__(self, one_shot: "Callable[[], Awaitable[V]]", open_session: "Callable[[], Awaitable[S]]",
                 on_exit: "Callable[[], Awaitable[None]] | None" = None) -> None:
        self._one_shot = one_shot
        self._open = open_session
        self._on_exit = on_exit
        self._session: "S | None" = None

    def __await__(self) -> "Generator[object, None, V]":
        return (yield from self._one_shot().__await__())

    async def __aenter__(self) -> S:
        self._session = await self._open()
        return self._session

    async def __aexit__(self, *_exc: object) -> None:
        try:
            if self._session is not None:
                await self._session.aclose()
        finally:
            if self._on_exit is not None:
                await self._on_exit()


class _Keep:
    """The 'unchanged' sentinel for :meth:`Profile.with_` (so ``None`` can be passed explicitly)."""


_KEEP = _Keep()


@dataclass(frozen=True)
class Profile:
    """A reusable transport identity: proxy + fingerprint + default headers + whether to drive a
    browser. Combine once, inherit with ``.with_(...)``; a per-call ``fetch`` kwarg overrides it."""

    proxy: "str | Proxy | None" = None
    fingerprint: "bool | Fingerprint" = False
    headers: "dict[str, str]" = field(default_factory=dict)
    browser: bool = False

    def with_(self, *, proxy: "str | Proxy | None | _Keep" = _KEEP,
              fingerprint: "bool | Fingerprint | _Keep" = _KEEP,
              headers: "dict[str, str] | _Keep" = _KEEP,
              browser: "bool | _Keep" = _KEEP) -> "Profile":
        """A copy with some slots overridden (the rest inherited) -- adjust a base profile."""
        return Profile(
            proxy=self.proxy if isinstance(proxy, _Keep) else proxy,
            fingerprint=self.fingerprint if isinstance(fingerprint, _Keep) else fingerprint,
            headers=self.headers if isinstance(headers, _Keep) else headers,
            browser=self.browser if isinstance(browser, _Keep) else browser,
        )

    def fetcher(self) -> "BrowserFetcher | HttpFetcher":
        """The backend this transport identity describes -- so a fetch profile can be used directly
        as a tier in a resolve profile's escalation ladder (an HTTP tier, a browser tier, ...)."""
        if self.browser:
            return BrowserFetcher(proxy=self.proxy, fingerprint=self.fingerprint)
        return HttpFetcher(proxy=self.proxy, fingerprint=self.fingerprint)


_EMPTY = Profile()


def as_request(request: "Request | str", headers: "dict[str, str]") -> Request:
    """A ``Request`` from a URL string or a ready request, with profile headers merged in (an
    explicit request's own headers win)."""
    req = Request(url=request) if isinstance(request, str) else request
    return req.model_copy(update={"headers": {**headers, **req.headers}}) if headers else req


def fetch(request: "Request | str", *, browser: bool = False, profile: "Profile | None" = None,
          proxy: "str | Proxy | None" = None, fingerprint: "bool | Fingerprint" = False) -> "Entry[Snapshot, Session]":
    """Fetch ``request`` (a URL or a :class:`Request`). ``await`` it for a one-shot Snapshot, or
    ``async with fetch(...) as session:`` for a live session (a browser session is navigated to the
    request and owns its page; an HTTP session holds a cookie jar). ``profile`` supplies the
    transport identity; ``browser`` / ``proxy`` / ``fingerprint`` override it per call."""
    prof = profile or _EMPTY
    eff = Profile(
        proxy=proxy if proxy is not None else prof.proxy,
        fingerprint=fingerprint if fingerprint is not False else prof.fingerprint,
        headers=prof.headers,
        browser=browser or prof.browser,
    )
    backend = eff.fetcher()
    req = as_request(request, eff.headers)

    async def one_shot() -> Snapshot:
        try:
            return await backend.fetch(req)
        finally:
            await backend.aclose()

    async def open_session() -> Session:
        session = await backend.session()
        if isinstance(session, BrowserSession):  # position the page at the request; then click/snapshot
            await session.goto(req)
        return session

    return Entry(one_shot, open_session, backend.aclose)


__all__ = ["fetch", "Entry", "Profile", "as_request"]
