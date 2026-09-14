"""The HTTP transport client -- one ``httpx.AsyncClient`` and the response
sniffing helpers that go with it."""

from __future__ import annotations

from typing import Any, Literal

import httpx

from .base import Client, ClientFactory


class HTTPXClient(Client):
    """One ``httpx.AsyncClient``. Cookies are passed per request and the jar is
    cleared on ``reset`` so a recycled client never leaks cookies across leases
    (session cookie state lives on the session, not the transport)."""

    kind = "http"

    def __init__(self, *, verify: bool = True, proxy: str | None = None) -> None:
        self._httpx = httpx.AsyncClient(
            follow_redirects=True, verify=verify, proxy=proxy
        )

    async def send(
        self,
        ref: Any,
        *,
        headers: dict[str, str],
        cookies: dict[str, str],
        timeout: float,
        retries: int = 0,
    ) -> httpx.Response:
        """Perform the request described by ``ref`` (retries transport errors)."""
        for name, value in cookies.items():  # jar is cleared on reset()
            self._httpx.cookies.set(name, value)
        last: httpx.TransportError | None = None
        for _ in range(retries + 1):
            try:
                return await self._httpx.request(
                    ref.method.upper(),
                    ref.dispatch("url"),
                    headers=headers or None,
                    content=ref.body,
                    json=ref.json_body,
                    data=ref.form,
                    follow_redirects=ref.follow_redirects,
                    timeout=ref.timeout if ref.timeout is not None else timeout,
                )
            except httpx.TransportError as exc:
                last = exc
        assert last is not None
        raise last

    async def fetch(
        self,
        ref: Any,
        *,
        headers: dict[str, str],
        cookies: dict[str, str],
        timeout: float,
    ) -> "tuple[Any, httpx.Response | None]":
        """Fetch ``ref`` into a ``(Document, response)`` -- the http client's
        whole job: perform the request, interpret the response (sniff kind /
        charset, capture Set-Cookie) and shape it into a document. Never raises: a
        transport failure or a non-2xx status is recorded as ``doc.error`` (the
        caller decides whether to retry or surface it). ``response`` is ``None`` on
        a transport failure, else the raw ``httpx.Response`` (for redirect history
        / ``Retry-After``)."""
        import time

        from ..core.document import Document
        from ..errors import error_for

        start = time.monotonic()
        try:
            resp = await self.send(
                ref, headers=headers, cookies=cookies, timeout=timeout
            )
        except Exception as exc:  # transport failure -> a not-ok document
            doc = Document(
                url=ref.dispatch("url"),
                status_code=0,
                elapsed=time.monotonic() - start,
                error=error_for(0, str(exc)),
            )
            return doc, None
        doc = Document(
            url=ref.dispatch("url"),
            final_url=str(resp.url),
            kind=sniff_kind(resp.headers.get("content-type"), resp.content),
            content=resp.content,
            status_code=resp.status_code,
            response_headers=dict(resp.headers),
            elapsed=time.monotonic() - start,
            encoding=charset_of(resp.headers.get("content-type")),
        )
        doc._set_cookies = dict(resp.cookies)  # httpx parses Set-Cookie correctly
        if not (200 <= resp.status_code < 300):
            doc.error = error_for(resp.status_code)
        return doc, resp

    async def reset(self) -> None:
        self._httpx.cookies.clear()

    async def aclose(self) -> None:
        await self._httpx.aclose()


class HTTPXFactory(ClientFactory):
    kind = "http"

    def __init__(self, *, verify: bool = True, proxy: str | None = None) -> None:
        self.verify = verify
        self.proxy = proxy

    async def create(self) -> HTTPXClient:
        return HTTPXClient(verify=self.verify, proxy=self.proxy)


# -- response sniffing (used when a fetched response is turned into a document) --


def sniff_kind(
    content_type: str | None, content: bytes
) -> Literal["html", "json", "xml", "binary"]:
    """Content-type header first, leading bytes as fallback."""
    mime = (content_type or "").split(";")[0].strip().lower()
    if "html" in mime:
        return "html"
    if mime == "application/json" or mime.endswith("+json"):
        return "json"
    if mime in ("text/xml", "application/xml") or mime.endswith("+xml"):
        return "xml"
    if mime.startswith("text/"):  # text/plain etc. -> parse as html
        return "html"
    head = content[:256].lstrip().lower()
    if head.startswith((b"<!doctype", b"<html")):
        return "html"
    if head.startswith(b"<?xml"):
        return "xml"
    if head.startswith((b"{", b"[")):
        return "json"
    if head.startswith(b"<"):
        return "html"
    return "binary"


def charset_of(content_type: str | None) -> str | None:
    for part in (content_type or "").split(";")[1:]:
        name, _, value = part.partition("=")
        if name.strip().lower() == "charset":
            return value.strip().strip('"').lower() or None
    return None


__all__ = ["HTTPXClient", "HTTPXFactory", "sniff_kind", "charset_of"]
