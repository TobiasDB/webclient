"""web.parse -- interpretation: ``bytes -> Document``. Depends only on web.kernel.

Purely content: sniff the kind, decode the charset, build the tree, and expose the utilities to
find / extract -- ``select`` / ``select_all`` / ``links`` / ``json`` on a :class:`Document`, and
``text`` / ``attr`` / nested ``select`` on an :class:`Element`. It knows nothing of transport --
no Snapshot, no status/headers -- so it runs on any bytes, in isolation:

    from web.parse import parse
    doc = parse(b"<h1>hi</h1>", content_type="text/html")

``url`` is only the base for resolving relative links. The ``Snapshot -> Document`` bridge lives
in web.resolve, and it just hands over the bytes.
"""

from __future__ import annotations

from .document import Document, Element
from .sniff import Kind, sniff_charset, sniff_kind


def parse(content: bytes, *, content_type: str | None = None, url: str = "") -> Document:
    """Interpret bytes into a :class:`Document`: sniff the kind and charset from ``content_type``
    + the bytes, keeping ``url`` as the base for relative-link resolution."""
    return Document(
        content=content,
        kind=sniff_kind(content_type, content),
        url=url,
        encoding=sniff_charset(content_type, content),
    )


__all__ = ["parse", "Document", "Element", "Kind", "sniff_kind", "sniff_charset"]
