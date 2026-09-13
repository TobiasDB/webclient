"""HTTP response sniffing helpers (the request itself is issued by
``engine.clients.HTTPXClient``)."""

from __future__ import annotations

from typing import Literal


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
