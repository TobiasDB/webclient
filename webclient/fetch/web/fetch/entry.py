"""The functional entry: ``fetch(url)`` -- one-shot OR a session, from ONE call.

Two shapes from one function, no object to construct and no ``try/finally``:

    snap = await fetch(url)                         # one-shot Snapshot
    async with fetch(url, browser=True) as page:    # a live session (owns the page)
        await page.click("#more"); shot = await page.snapshot()

:class:`Entry` is the dual awaitable / async-context-manager this returns (reused by the resolve
layer for ``resolve()``). The transport identity (:class:`~web.fetch.base.Profile`) and the backend
pool (:class:`~web.fetch.base.ClientPool`) live in :mod:`web.fetch.base`.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Generator
from typing import Generic, Protocol, TypeVar, runtime_checkable

from .base import ClientPool, Profile, default_pool
from .browser import BrowserSession
from .fingerprint import Fingerprint
from .models import Request, Session, Snapshot
from .proxy import Proxy

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

    def __init__(
        self,
        one_shot: "Callable[[], Awaitable[V]]",
        open_session: "Callable[[], Awaitable[S]]",
        on_exit: "Callable[[], Awaitable[None]] | None" = None,
    ) -> None:
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


def as_request(request: "Request | str", headers: "dict[str, str]") -> Request:
    """A ``Request`` from a URL string or a ready request, with profile headers merged in (an
    explicit request's own headers win)."""
    req = Request(url=request) if isinstance(request, str) else request
    return req.model_copy(update={"headers": {**headers, **req.headers}}) if headers else req


def fetch(
    request: "Request | str",
    *,
    browser: bool = False,
    profile: "Profile | None" = None,
    proxy: "str | Proxy | None" = None,
    fingerprint: "bool | Fingerprint" = False,
    pool: "ClientPool | None" = None,
) -> "Entry[Snapshot, Session]":
    """Fetch ``request`` (a URL or a :class:`Request`). ``await`` it for a one-shot Snapshot, or
    ``async with fetch(...) as session:`` for a live session (a browser session is navigated to the
    request and owns its page; an HTTP session holds a cookie jar). ``profile`` supplies the
    transport identity; ``browser`` / ``proxy`` / ``fingerprint`` override it per call. The backend
    is LEASED from ``pool`` (or the process :func:`~web.fetch.base.default_pool`) -- shared and
    reused, so the browser is not relaunched per fetch. The pool owns the backend; only the session
    (its page / cookie jar) is closed on exit."""
    prof = profile or Profile()
    # Override ONLY the per-call args, inheriting every other transport slot from the profile via
    # `with_` -- a bare `Profile(...)` here silently dropped `executable_path` / `channel` /
    # `headless` / `impersonate` / `stealth`, so a pinned browser binary (WEB_BROWSER_PATH, the
    # `chrome` channel) was discarded and the fetch never launched the requested browser.
    eff = prof.with_(
        proxy=proxy if proxy is not None else prof.proxy,
        fingerprint=fingerprint if fingerprint is not False else prof.fingerprint,
        browser=browser or prof.browser,
    )
    backend = (pool or default_pool()).lease(eff)
    req = as_request(request, eff.headers)

    async def one_shot() -> Snapshot:
        return await backend.fetch(req)  # the pool owns the backend -- do not close it here

    async def open_session() -> Session:
        session = await backend.session()  # a fresh context/page (browser) or cookie jar (http)
        if isinstance(
            session, BrowserSession
        ):  # position the page at the request; then click/snapshot
            await session.goto(req)
        return session

    return Entry(
        one_shot, open_session
    )  # on exit: close the session only; the pooled backend lives


__all__ = ["fetch", "Entry", "as_request"]
