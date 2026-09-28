"""Sniffing: decide a resource's KIND and text ENCODING -- the first thing parse does that
fetch deliberately does not. Kind is taken from the ``Content-Type`` when it is informative,
else from the leading bytes; encoding from a ``charset=`` parameter, else UTF-8.
"""

from __future__ import annotations

import codecs
import re
from typing import Literal

Kind = Literal["html", "json", "xml", "text", "binary"]

_CHARSET = re.compile(rb"charset=([\w-]+)", re.I)


def _known(name: str) -> str:
    """``name`` if it is a registered codec, else ``"utf-8"`` -- so a mislabelled/typo charset
    (``charset=bogus``) can never make ``Document.text``'s ``decode()`` raise LookupError."""
    try:
        codecs.lookup(name)
        return name
    except LookupError:
        return "utf-8"


def sniff_kind(content_type: str | None, content: bytes) -> Kind:
    """The resource kind. A declared, specific ``Content-Type`` wins; otherwise the leading
    bytes decide (``{``/``[`` -> json, an html marker -> html, ``<`` -> xml, decodable ->
    text, else binary). ``application/octet-stream`` is treated as "unknown" and sniffed."""
    ct = (content_type or "").split(";")[0].strip().lower()
    if ct and ct != "application/octet-stream":
        if ct in ("text/html", "application/xhtml+xml"):
            return "html"
        if ct.endswith("json") or ct.endswith("+json"):
            return "json"
        if ct.endswith("xml") or ct.endswith("+xml"):
            return "xml"
        if ct.startswith("text/"):
            return "text"
        return "binary"
    head = content[:512].lstrip()
    if head[:1] in (b"{", b"["):
        return "json"
    low = head[:200].lower()
    if low.startswith(b"<!doctype html") or b"<html" in low:
        return "html"
    if head[:1] == b"<":
        return "xml"
    try:
        content[:512].decode("utf-8")
        return "text"
    except UnicodeDecodeError:
        return "binary"


def sniff_charset(content_type: str | None, content: bytes) -> str:
    """The text encoding: a ``charset=`` on the Content-Type, else a ``<meta charset>`` in the
    first bytes, else UTF-8."""
    if content_type and (m := _CHARSET.search(content_type.encode())):
        return _known(m.group(1).decode("ascii", "replace").lower())
    if m := _CHARSET.search(content[:1024]):
        return _known(m.group(1).decode("ascii", "replace").lower())
    return "utf-8"


__all__ = ["Kind", "sniff_kind", "sniff_charset"]
