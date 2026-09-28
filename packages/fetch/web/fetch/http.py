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
from .request import Request
from .snapshot import Snapshot


#: browser-like default headers a ``fingerprint`` http backend sends (a first-cut fingerprint;
#: real TLS/JA3 impersonation is a heavier backend -- swap the httpx transport for curl_cffi/
#: tls-client and this class is where it plugs in). The request's own headers still win.
_FINGERPRINT: dict[str, str] = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Sec-Ch-Ua": '"Chromium";v="124", "Not-A.Brand";v="99"',
    "Upgrade-Insecure-Requests": "1",
}


class HttpFetcher:
    """A static HTTP :class:`~web.fetch.base.Fetcher` over httpx. ``proxy`` routes through a
    proxy; ``fingerprint`` sends browser-like headers. It just fetches -- the tier/escalation
    ladder that CHOOSES between such backends is resolve's policy, not fetch's."""

    def __init__(self, *, verify: bool = True, proxy: str | None = None, fingerprint: bool = False) -> None:
        self._client = httpx.AsyncClient(verify=verify, proxy=proxy)
        self._base_headers = _FINGERPRINT if fingerprint else {}

    async def fetch(self, request: Request) -> Snapshot:
        start = time.perf_counter()
        headers = {**self._base_headers, **request.headers} if self._base_headers else request.headers
        try:
            resp = await self._client.request(
                request.method.upper(),
                request.url,
                headers=headers or None,
                cookies=request.cookies or None,
                content=request.body,
                follow_redirects=request.follow_redirects,
                timeout=request.timeout,
            )
        except Exception as exc:  # never raise for a transport failure; classify into a stable
            # failure mode. (CancelledError is a BaseException, so cancellation still propagates.)
            return Snapshot(
                request=request,
                url=request.url,
                elapsed=time.perf_counter() - start,
                error=classify(exc, url=request.url),
            )
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

    async def aclose(self) -> None:
        await self._client.aclose()


__all__ = ["HttpFetcher"]
