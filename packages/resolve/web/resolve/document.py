"""The ``Snapshot -> Document`` bridge -- owned by resolve, which has both fetch and parse.

It simply takes the bytes from the Snapshot (and its Content-Type, to sniff, and its final URL,
as the link base) and hands them to parse. The Snapshot's other information -- status, headers,
redirects, captured events, error -- is used INTERNALLY by resolve (retry, escalation, signals),
not carried onto the Document, which is just the parsed content.
"""

from __future__ import annotations

from web.fetch import Snapshot
from web.parse import Document, parse


def document(snap: Snapshot) -> Document:
    """Parse the Snapshot's bytes into a :class:`~web.parse.Document` (Content-Type looked up
    case-insensitively; the final URL is the link base)."""
    ci = {k.lower(): v for k, v in snap.headers.items()}
    return parse(
        snap.content,
        content_type=ci.get("content-type"),
        url=snap.url or snap.request.url,
    )


__all__ = ["document"]
