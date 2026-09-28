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

from web.kernel import err

from .request import Request
from .snapshot import Snapshot


class HttpFetcher:
    """A static HTTP :class:`~web.fetch.base.Fetcher` over httpx."""

    def __init__(self, *, verify: bool = True, proxy: str | None = None) -> None:
        self._client = httpx.AsyncClient(verify=verify, proxy=proxy)

    async def fetch(self, request: Request) -> Snapshot:
        start = time.perf_counter()
        try:
            resp = await self._client.request(
                request.method.upper(),
                request.url,
                headers=request.headers or None,
                cookies=request.cookies or None,
                content=request.body,
                follow_redirects=request.follow_redirects,
                timeout=request.timeout,
            )
        except httpx.HTTPError as exc:
            return Snapshot(
                request=request,
                url=request.url,
                elapsed=time.perf_counter() - start,
                error=err("fetch.transport", str(exc), url=request.url),
            )
        set_cookies: dict[str, str] = {}
        for hop in (*resp.history, resp):  # Set-Cookie from every hop, not just the final one
            set_cookies.update(dict(hop.cookies))
        return Snapshot(
            request=request,
            url=str(resp.url),
            status=resp.status_code,
            headers=dict(resp.headers),
            content=resp.content,
            elapsed=time.perf_counter() - start,
            set_cookies=set_cookies,
            redirects=[str(h.url) for h in resp.history],
        )

    async def aclose(self) -> None:
        await self._client.aclose()


__all__ = ["HttpFetcher"]
