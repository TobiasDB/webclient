"""web.parse -- interpretation: ``bytes | Snapshot -> Document``.

The layer fetch deliberately stops short of: it sniffs the content kind, decodes the charset,
and parses the bytes into a :class:`Document` you can read (``text`` / ``select`` / ``links`` /
``json``). Two entry points:

    from web.parse import parse_bytes, parse
    doc = parse_bytes(b"<h1>hi</h1>", content_type="text/html")   # fetch-free, works on raw bytes
    doc = parse(snapshot)                                          # from a web.fetch.Snapshot

``parse_bytes`` needs nothing but bytes, so parse is usable in isolation; ``parse`` is the thin
adapter that reads a Snapshot's fields. Depends on web.kernel and web.fetch (for the Snapshot
contract) -- lower layers only.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from web.kernel import WebError

from .document import Document, Element
from .sniff import Kind, sniff_charset, sniff_kind

if TYPE_CHECKING:
    from web.fetch import Snapshot


def parse_bytes(
    content: bytes,
    *,
    content_type: str | None = None,
    url: str = "",
    status: int = 0,
    headers: dict[str, str] | None = None,
    error: "WebError | None" = None,
) -> Document:
    """Interpret raw bytes into a :class:`Document` -- the fetch-free core. Sniffs the kind
    and charset from ``content_type`` + the bytes."""
    return Document(
        content=content,
        kind=sniff_kind(content_type, content),
        url=url,
        status=status,
        headers=headers,
        encoding=sniff_charset(content_type, content),
        error=error,
    )


def parse(snapshot: "Snapshot") -> Document:
    """Interpret a :class:`web.fetch.Snapshot` into a :class:`Document` -- the fetch -> parse
    step. Reads the response's Content-Type (case-insensitively) to sniff, and carries the
    snapshot's url / status / headers / error onto the document."""
    ci = {k.lower(): v for k, v in snapshot.headers.items()}
    return parse_bytes(
        snapshot.content,
        content_type=ci.get("content-type"),
        url=snapshot.url or snapshot.request.url,
        status=snapshot.status,
        headers=snapshot.headers,
        error=snapshot.error,
    )


__all__ = ["parse", "parse_bytes", "Document", "Element", "Kind", "sniff_kind", "sniff_charset"]
