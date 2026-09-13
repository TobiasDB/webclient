"""HTTP transport: httpx request execution plus response sniffing helpers."""
from __future__ import annotations

from typing import Literal

import httpx

from ..core.reference_core import ReferenceCore


async def request(client: httpx.AsyncClient, ref: ReferenceCore, *,
                  headers: dict[str, str], cookies: dict[str, str],
                  timeout: float, retries: int) -> httpx.Response:
    """Perform the request described by ``ref`` with pre-merged headers and
    cookies. Retries transport errors only, immediately, ``retries`` times."""
    last_error: httpx.TransportError | None = None
    for _ in range(retries + 1):
        try:
            resp = await client.request(
                ref.method.upper(),
                ref.dispatch("url"),
                headers=headers or None,
                cookies=cookies or None,
                content=ref.body,
                json=ref.json_body,
                data=ref.form,
                follow_redirects=ref.follow_redirects,
                timeout=ref.timeout if ref.timeout is not None else timeout,
            )
            # keep the shared client's jar empty: cookies are supplied
            # per-request and Set-Cookie is captured from response headers, so
            # nothing leaks across requests or between sessions.
            client.cookies.clear()
            return resp
        except httpx.TransportError as exc:
            last_error = exc
    assert last_error is not None
    raise last_error


def sniff_kind(content_type: str | None,
               content: bytes) -> Literal["html", "json", "xml", "binary"]:
    """Content-type header first, leading bytes as fallback."""
    mime = (content_type or "").split(";")[0].strip().lower()
    if "html" in mime:
        return "html"
    if mime == "application/json" or mime.endswith("+json"):
        return "json"
    if mime in ("text/xml", "application/xml") or mime.endswith("+xml"):
        return "xml"
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
