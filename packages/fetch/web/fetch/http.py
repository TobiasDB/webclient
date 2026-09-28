"""``HttpFetcher`` -- the static HTTP transport (one ``httpx.AsyncClient``).

The whole job: perform the request and record the response as a :class:`Snapshot`. It never
raises for a transport failure -- a connect/timeout error becomes ``snapshot.error`` -- and it
never interprets the body (no sniffing, no decoding; that is parse). Cookies are passed per
request; Set-Cookie is aggregated across every redirect hop (an auth flow that sets its cookie
on a 302 is not lost).
"""

from __future__ import annotations

import time

import httpx

from web.kernel import emit

from .errors import classify
from .events import FetchEvent
from .fingerprint import Fingerprint, as_fingerprint
from .proxy import Proxy, as_proxy
from .request import Request
from .snapshot import Snapshot


class HttpFetcher:
    """A static HTTP backend over httpx. ``proxy`` routes through a proxy; ``fingerprint`` (``True``
    or a :class:`~web.fetch.fingerprint.Fingerprint`) sends a coherent identity's headers. Its
    ``fetch`` is a stateless one-shot; ``session()`` opens a stateful :class:`HttpSession` whose
    cookie jar persists across fetches. It just fetches -- the tier/escalation ladder is resolve's
    policy, not fetch's. (TLS/JA3 impersonation is a heavier transport swap -- curl_cffi/tls-client
    -- and this class is where it would plug in.)"""

    def __init__(
        self, *, verify: bool = True, proxy: "str | Proxy | None" = None,
        fingerprint: "bool | Fingerprint" = False,
    ) -> None:
        self._verify = verify
        self._proxy = as_proxy(proxy)
        self._client = httpx.AsyncClient(verify=verify, proxy=self._proxy.httpx() if self._proxy else None)
        fp = as_fingerprint(fingerprint)
        self._base_headers = fp.http_headers() if fp else {}

    async def fetch(self, request: Request) -> Snapshot:
        self._client.cookies.clear()  # stateless one-shot: no cookie carry-over between fetches
        return await _perform(self._client, request, self._base_headers)

    async def session(self) -> "HttpSession":
        """A stateful session over its OWN httpx client (a persistent cookie jar). Async so every
        backend's ``session()`` has one shape (the browser's must be)."""
        return HttpSession(httpx.AsyncClient(verify=self._verify, proxy=self._proxy.httpx() if self._proxy else None), self._base_headers)

    async def aclose(self) -> None:
        await self._client.aclose()


class HttpSession:
    """A stateful HTTP session: it OWNS an httpx client whose cookie jar persists across fetches
    (so a login on one fetch carries to the next). Close it to release the client."""

    def __init__(self, client: httpx.AsyncClient, base_headers: dict[str, str]) -> None:
        self._client = client
        self._base_headers = base_headers

    async def fetch(self, request: Request) -> Snapshot:
        return await _perform(self._client, request, self._base_headers)  # jar persists (not cleared)

    async def aclose(self) -> None:
        await self._client.aclose()


async def _perform(client: httpx.AsyncClient, request: Request, base_headers: dict[str, str]) -> Snapshot:
    """Perform one request on ``client`` and build the Snapshot -- shared by the backend and the
    session. Never raises for a transport failure (it is classified onto ``snapshot.error``)."""
    start = time.perf_counter()
    headers = {**base_headers, **request.headers} if base_headers else request.headers
    try:
        resp = await client.request(
            request.method.upper(),
            request.url,
            headers=headers or None,
            cookies=request.cookies or None,
            content=request.body,
            follow_redirects=request.follow_redirects,
            timeout=request.timeout,
        )
    except Exception as exc:  # classify; CancelledError is a BaseException, so it still propagates
        return Snapshot(request=request, url=request.url, elapsed=time.perf_counter() - start,
                        error=classify(exc, url=request.url))
    set_cookies: dict[str, str] = {}
    for hop in (*resp.history, resp):  # Set-Cookie from every hop, not just the final one
        set_cookies.update(dict(hop.cookies))
    snap = Snapshot(
        request=request,
        url=str(resp.url),
        status=resp.status_code,
        headers=dict(resp.headers),
        content=resp.content,
        elapsed=time.perf_counter() - start,
        set_cookies=set_cookies,
        redirects=[str(h.url) for h in resp.history],
    )
    emit(FetchEvent(url=snap.url, status=snap.status, elapsed=snap.elapsed, source="http"))
    return snap


__all__ = ["HttpFetcher", "HttpSession"]
