"""``ImpersonateFetcher`` -- an HTTP transport that IMPERSONATES a real browser's TLS + HTTP/2
fingerprint (curl_cffi), closing the network-layer tell that plain httpx leaves.

ANTI-BOT.md §2.1: a stock HTTP client (httpx on OpenSSL, Go's ``net/http``) emits ONE fixed JA3/JA4
+ HTTP/2 fingerprint, identical on every request and nothing like a browser's BoringSSL/NSS
handshake -- an instant blocklist entry read before a byte of content. curl_cffi rebuilds curl
against the browser's actual TLS stack, so the ClientHello (cipher + extension order) and the HTTP/2
SETTINGS / pseudo-header order are byte-for-byte a real Chrome's. This is **rung 2** of the evasion
ladder (§5): the cheap tier that clears network fingerprinting without the cost of a real browser.
It runs NO JavaScript, so a JS / proof-of-work challenge (§4) still needs the browser rungs above it.

curl_cffi is an optional extra (a compiled, patched libcurl); it is imported lazily so importing
web.fetch never requires it. This backend implements the same ``fetch(request) -> Snapshot`` /
``session`` / ``aclose`` protocol as :class:`~web.fetch.http.HttpFetcher`, so resolve slots it into
the escalation ladder like any other tier -- fetch stays flag-unaware.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping, Sequence
from typing import Protocol

from .bus import emit
from .errors import classify
from .models import FetchEvent, Request, Snapshot
from .proxy import Proxy, as_proxy


# Small structural views over exactly what we use of curl_cffi (which is an optional compiled extra
# with its own concrete types) -- so this module type-checks without importing it and without `Any`.
class _Jar(Protocol):
    def clear(self) -> None: ...


class _Resp(Protocol):
    status_code: int
    content: "bytes | None"
    url: object
    headers: "Mapping[str, str]"
    cookies: "Mapping[str, str]"
    history: "Sequence[_Resp]"


class _CurlSession(Protocol):
    cookies: _Jar

    async def request(self, method: str, url: str, **kwargs: object) -> _Resp: ...

    async def close(self) -> None: ...


#: the browser identity to impersonate. curl_cffi ships versioned presets (``chrome124`` …); the
#: bare ``chrome`` tracks its newest build -- and currency matters, since a STALE preset against the
#: current Chrome is itself a signal (§5). Override for a pinned version or a different browser.
_DEFAULT_IMPERSONATE = "chrome"


def _new_session(impersonate: str, verify: bool, proxy: "Proxy | None") -> _CurlSession:
    from curl_cffi import (
        AsyncSession,  # optional compiled extra -- lazy so web.fetch never needs it
    )

    # curl_cffi's concrete AsyncSession narrows `request(method=...)` to a Literal of verbs, so it
    # doesn't structurally satisfy our looser view -- bridge it here, at the one optional-dep seam.
    session: _CurlSession = AsyncSession(  # type: ignore[assignment]
        impersonate=impersonate, verify=verify, proxy=proxy.httpx() if proxy else None
    )
    return session


class ImpersonateFetcher:
    """A TLS/HTTP2-impersonating HTTP backend over curl_cffi. ``impersonate`` names the browser
    preset (default a recent Chrome); ``proxy`` routes through a proxy; ``verify`` toggles TLS
    verification. ``fetch`` is a stateless one-shot (its cookie jar is cleared per call); ``session``
    opens a stateful :class:`ImpersonateSession` whose jar persists."""

    def __init__(
        self,
        *,
        impersonate: str = _DEFAULT_IMPERSONATE,
        proxy: "str | Proxy | None" = None,
        verify: bool = True,
    ) -> None:
        self._impersonate = impersonate
        self._proxy = as_proxy(proxy)
        self._verify = verify
        self._session: "_CurlSession | None" = None
        self._loop: "asyncio.AbstractEventLoop | None" = None

    def _ready(self) -> _CurlSession:
        # curl_cffi's AsyncSession binds to the event loop it was created on. This backend is POOLED
        # (one per profile, process-wide), so it can be reused under a DIFFERENT loop -- e.g. a second
        # ``asyncio.run`` in one process -- which would otherwise crash with a cross-loop future.
        # Rebind to the current loop when it changes (httpx tolerates this natively; curl_cffi does
        # not). The stale session's loop is gone, so we drop it without awaiting its close (that would
        # need the old loop); curl_cffi frees the handle on GC.
        loop = asyncio.get_running_loop()
        if self._session is None or self._loop is not loop:
            self._session = _new_session(self._impersonate, self._verify, self._proxy)
            self._loop = loop
        return self._session

    async def fetch(self, request: Request) -> Snapshot:
        session = self._ready()
        try:
            session.cookies.clear()  # stateless one-shot: no cookie carry-over between fetches
        except Exception:  # a jar that refuses to clear must not sink the fetch
            pass
        return await _perform(session, request)

    async def session(self) -> "ImpersonateSession":
        """A stateful session over its OWN curl_cffi session (a persistent cookie jar), so a login on
        one fetch carries to the next. Async so every backend's ``session()`` has one shape."""
        return ImpersonateSession(_new_session(self._impersonate, self._verify, self._proxy))

    async def aclose(self) -> None:
        if self._session is not None:
            # close only on the loop the session is bound to; on a mismatch (or no loop) drop it and
            # let GC free the handle -- awaiting close on the wrong loop would itself raise.
            try:
                if self._loop is asyncio.get_running_loop():
                    await self._session.close()
            except RuntimeError:
                pass
            self._session = None
            self._loop = None


class ImpersonateSession:
    """A stateful impersonating session: it OWNS a curl_cffi session whose cookie jar persists."""

    def __init__(self, session: _CurlSession) -> None:
        self._session = session

    async def fetch(self, request: Request) -> Snapshot:
        return await _perform(self._session, request)  # jar persists (not cleared)

    async def aclose(self) -> None:
        await self._session.close()


async def _perform(session: _CurlSession, request: Request) -> Snapshot:
    """Perform one request on ``session`` and build the Snapshot -- shared by the backend and the
    session. Never raises for a transport failure (it is classified onto ``snapshot.error``).
    curl_cffi advertises AND decodes the browser's real Accept-Encoding (br/zstd included), so the
    body comes back decoded -- no Accept-Encoding stripping is needed here (unlike the httpx path).
    """
    start = time.perf_counter()
    try:
        resp = await session.request(
            request.method.upper(),
            request.url,
            headers=dict(request.headers) or None,
            cookies=request.cookies or None,
            content=request.body,
            allow_redirects=request.follow_redirects,
            timeout=request.timeout,
        )
    except Exception as exc:  # classify; CancelledError is a BaseException, so it still propagates
        return Snapshot(
            request=request,
            url=request.url,
            elapsed=time.perf_counter() - start,
            error=classify(exc, url=request.url),
        )
    history = list(resp.history or ())
    set_cookies: dict[str, str] = {}
    for hop in (*history, resp):  # Set-Cookie from every redirect hop, not just the final one
        try:
            set_cookies.update({str(k): str(v) for k, v in dict(hop.cookies).items()})
        except Exception:
            pass
    snap = Snapshot(
        request=request,
        url=str(resp.url),
        status=resp.status_code,
        headers={str(k): str(v) for k, v in dict(resp.headers).items()},
        content=resp.content or b"",
        elapsed=time.perf_counter() - start,
        set_cookies=set_cookies,
        redirects=[str(h.url) for h in history],
    )
    emit(FetchEvent(url=snap.url, status=snap.status, elapsed=snap.elapsed, source="impersonate"))
    return snap


__all__ = ["ImpersonateFetcher", "ImpersonateSession"]
