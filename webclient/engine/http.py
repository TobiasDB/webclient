"""HTTP transport: httpx request execution plus response sniffing helpers."""
from __future__ import annotations

from typing import Literal

import httpx

from ..core.document import Reference


async def request(client: httpx.AsyncClient, ref: Reference, *,
                  headers: dict[str, str], cookies: dict[str, str],
                  timeout: float, retries: int) -> httpx.Response:
    """Perform the request described by ``ref`` with pre-merged headers and
    cookies. Retries transport errors only, immediately, ``retries`` times
    (ISSUES #20 -- full policy is post-v1)."""
    # The lease-exclusive client's jar carries the cookies for this request;
    # the pool clears it on release, so nothing leaks across sessions.
    for name, value in cookies.items():
        client.cookies.set(name, value)
    last_error: httpx.TransportError | None = None
    for _ in range(retries + 1):
        try:
            return await client.request(
                ref.method.upper(),
                ref.url,
                headers=headers or None,
                content=ref.body,
                json=ref.json_body,
                data=ref.form,
                follow_redirects=ref.follow_redirects,
                timeout=ref.timeout if ref.timeout is not None else timeout,
            )
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
