"""The HTTP transport client -- one ``httpx.AsyncClient`` and the response
sniffing helpers that go with it."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Literal, cast

import httpx

from .base import Client, ClientFactory

if TYPE_CHECKING:
    from ..core.document import Document
    from ..core.reference import Reference


log = logging.getLogger(__name__)


class HTTPXClient(Client):
    """One ``httpx.AsyncClient``. Cookies are passed per request and the jar is
    cleared on ``reset`` so a recycled client never leaks cookies across leases
    (session cookie state lives on the session, not the transport)."""

    kind = "http"

    def __init__(
        self, *, verify: bool = True, proxy: str | None = None, har: str | None = None
    ) -> None:
        if har:  # replay mode: every request is answered from the HAR, never the network
            from ..replay.har import HarTransport

            self._httpx = httpx.AsyncClient(follow_redirects=True, transport=HarTransport(har))
        else:
            self._httpx = httpx.AsyncClient(
                follow_redirects=True, verify=verify, proxy=proxy
            )

    async def send(
        self,
        ref: "Reference",
        *,
        headers: dict[str, str],
        cookies: dict[str, str],
        timeout: float,
    ) -> httpx.Response:
        """Perform the request described by ``ref`` and return the raw response. A transport failure
        RAISES: the caller (:meth:`fetch`) turns it into a retriable not-ok document, and the core
        resolve loop (``_afetch_core``) retries it under the one ``RetryPolicy`` -- so retry/backoff
        lives in a SINGLE place (idempotency is honoured there, via the retriable-status vocabulary),
        not duplicated in the transport."""
        for name, value in cookies.items():  # jar is cleared on reset()
            self._httpx.cookies.set(name, value)
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

    async def fetch(
        self,
        ref: "Reference",
        *,
        headers: dict[str, str],
        cookies: dict[str, str],
        timeout: float,
    ) -> "tuple[Document, httpx.Response | None]":
        """Fetch ``ref`` into a ``(Document, response)`` -- the http client's
        whole job: perform the request, interpret the response (sniff kind /
        charset, capture Set-Cookie) and shape it into a document. Never raises: a
        transport failure or a non-2xx status is recorded as ``doc.error`` (the
        caller decides whether to retry or surface it). ``response`` is ``None`` on
        a transport failure, else the raw ``httpx.Response`` (for redirect history
        / ``Retry-After``)."""
        import time

        from ..core.document import Document
        from ..kernel.errors import error_for

        start = time.monotonic()
        try:
            resp = await self.send(
                ref, headers=headers, cookies=cookies, timeout=timeout
            )
        except Exception as exc:  # transport failure -> a not-ok document
            log.warning("%s %s -> transport error: %s", ref.method.upper(), ref.dispatch("url"), exc)
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
            kind=sniff_kind(
                resp.headers.get("content-type"),
                resp.content,
                getattr(ref, "expect", None),
            ),
            content=resp.content,
            status_code=resp.status_code,
            response_headers=dict(resp.headers),
            elapsed=time.monotonic() - start,
            encoding=charset_of(resp.headers.get("content-type")),
        )
        # Set-Cookie from EVERY hop, not just the final response: an auth flow that
        # sets its session cookie on a 302 (then lands on the app) would otherwise
        # be lost. Each response's ``.cookies`` is httpx-parsed (so the Expires
        # comma is handled); aggregate across the redirect history + the final hop.
        set_cookies: dict[str, str] = {}
        for hop in (*resp.history, resp):
            set_cookies.update(dict(hop.cookies))
        doc._set_cookies = set_cookies
        if resp.status_code == 599 and resp.headers.get("x-webclient-har") == "miss":
            from ..kernel.errors import make

            doc.error = make("replay.har_miss", f"no HAR entry for {ref.method.upper()} {doc.url}")
        elif not (200 <= resp.status_code < 300):
            doc.error = error_for(resp.status_code)
        log.debug("%s %s -> %d %s %dB %.0fms", ref.method.upper(), doc.url, resp.status_code,
                  doc.kind, len(resp.content), (doc.elapsed or 0.0) * 1000)
        return doc, resp

    async def reset(self) -> None:
        """Clear the client's cookie jar before it is recycled, so a reused http client
        never carries one lease's cookies into the next."""
        self._httpx.cookies.clear()

    async def aclose(self) -> None:
        """Close the underlying httpx client."""
        await self._httpx.aclose()


class HTTPXFactory(ClientFactory):
    kind = "http"

    def __init__(
        self, *, verify: bool = True, proxy: str | None = None, har: str | None = None
    ) -> None:
        self.verify = verify
        self.proxy = proxy
        self.har = har  # replay: answer from this HAR instead of the network

    async def create(self) -> HTTPXClient:
        """Build a fresh httpx-backed client (its verify/proxy fixed by this factory)."""
        return HTTPXClient(verify=self.verify, proxy=self.proxy, har=self.har)


# -- response sniffing (used when a fetched response is turned into a document) --


def sniff_kind(
    content_type: str | None,
    content: bytes,
    hint: str | None = None,
) -> Literal["html", "json", "xml", "binary"]:
    """Classify a response into a document kind. An explicit ``hint`` (a Reference's
    ``expect``) wins outright -- the caller declared the kind, so a mislabelled or
    absent ``Content-Type`` cannot misguide it. Otherwise: the content-type header
    first, then the leading bytes as a fallback, then ``binary``."""
    if hint in ("html", "json", "xml", "binary"):
        return cast('Literal["html", "json", "xml", "binary"]', hint)
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
    """The ``charset=`` declared in a Content-Type header (lowercased), or ``None``."""
    for part in (content_type or "").split(";")[1:]:
        name, _, value = part.partition("=")
        if name.strip().lower() == "charset":
            return value.strip().strip('"').lower() or None
    return None


__all__ = ["HTTPXClient", "HTTPXFactory", "sniff_kind", "charset_of"]
