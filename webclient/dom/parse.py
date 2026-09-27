"""Stateless HTML-string / element helpers -- the parse half of :mod:`webclient.dom`.

Every module that reads a parsed tree, decodes or scans HTML text, or walks a JSON
value goes through here, so the primitives live in ONE place (no more four copies of
``_tag`` or three of ``norm``). Pure functions only: no ``webclient`` imports, no state,
nothing cached -- they take an element / string / bytes / parsed value and return a
value. A core that wants a *cached* parse (``Document._tree``) wraps :func:`parse_html`;
the caching is the core's, the parsing is here.

lxml is an OPTIONAL dependency (the ``local`` extra). This module never imports it at
load time -- the element helpers call methods on an element the caller already holds, and
:func:`parse_html` imports lxml lazily and degrades to ``None`` when it is absent -- so
``signals`` (which must import without lxml, for remote/slim installs) can rely on it.
"""

from __future__ import annotations

import re
from typing import Any

__all__ = [
    "norm", "tag", "local_name", "text_of",
    "sniff_charset", "decode_html", "parse_html",
    "visible_text", "strip_wc_attrs", "clean_href",
    ]


# --------------------------------------------------------------------------- #
# text + element helpers (no lxml import -- operate on a held element)
# --------------------------------------------------------------------------- #

def norm(text: str) -> str:
    """Collapse every run of whitespace to a single space and strip the ends."""
    return " ".join(text.split())


def tag(el: Any) -> str:
    """The element's lowercased tag name, or ``""`` for a non-element node (a comment /
    processing-instruction, whose ``.tag`` is not a string)."""
    return el.tag.lower() if isinstance(getattr(el, "tag", None), str) else ""


def local_name(el: Any) -> str:
    """The element's tag with any XML namespace prefix (``{ns}``) stripped, lowercased --
    so a namespaced ``{http://www.w3.org/1999/xhtml}nav`` reads as ``nav``."""
    return tag(el).rsplit("}", 1)[-1]


#: subtrees whose text is never visible page content -- their character data is code or
#: markup, not text a reader sees, so it must not leak into an element's extracted text.
_NON_TEXT_TAGS = frozenset({"script", "style", "template", "noscript"})


def _visible_itertext(el: Any) -> "Any":
    """Yield the visible text pieces of ``el`` in document order, pruning whole
    ``<script>``/``<style>``/``<template>``/``<noscript>`` subtrees and comment/PI bodies
    (their tails -- real text that follows them -- are kept). Unlike ``lxml``'s ``itertext``,
    which slurps a stylesheet's CSS or a script's source into the surrounding element."""
    if el.text:
        yield el.text
    for child in el:
        if isinstance(getattr(child, "tag", None), str):  # a real element, not a comment/PI
            if child.tag.lower() not in _NON_TEXT_TAGS:
                yield from _visible_itertext(child)
        if child.tail:                                     # text AFTER a pruned/comment node
            yield child.tail


def text_of(el: Any, *, own: bool = False) -> str:
    """The element's visible text, whitespace-normalised. ``own`` restricts it to the node's
    DIRECT text (its own text plus its children's tails), excluding descendant elements'
    text; otherwise all descendant text is included. Text inside ``<script>``/``<style>`` and
    similar non-text subtrees, and comment bodies, is never included (it is code, not text)."""
    if own:
        parts = [el.text or ""] + [c.tail or "" for c in el]
        return norm("".join(parts))
    return norm("".join(_visible_itertext(el)))


# --------------------------------------------------------------------------- #
# HTML-string helpers
# --------------------------------------------------------------------------- #

_CHARSET_RE = re.compile(rb"""(?:charset|encoding)\s*=\s*["']?\s*([A-Za-z0-9_\-]+)""", re.I)
#: our internal correlation stamps (``data-wc-*``): never shown to a user or used as a
#: selector, and stripped from any HTML handed back as output.
_WC_ATTR = re.compile(r'\s+data-wc-[\w-]+="[^"]*"')
#: script/style blocks (dropped whole) and any remaining tag, for a rough text extraction.
_SCRIPT_STYLE = re.compile(r"<(script|style)\b[^>]*>.*?</\1>", re.I | re.S)
_ANY_TAG = re.compile(r"<[^>]+>")


def sniff_charset(raw: bytes) -> str | None:
    """The in-document charset from a ``<meta>`` declaration or BOM in the first 2 KB, else
    ``None``. Mirrors a browser's encoding prescan; only consulted when no HTTP charset was
    sent."""
    if raw[:3] == b"\xef\xbb\xbf":
        return "utf-8"
    m = _CHARSET_RE.search(raw[:2048])
    return m.group(1).decode("ascii", "ignore") if m else None


def decode_html(raw: bytes, encoding: str | None = None) -> str:
    """Decode HTML bytes to text using the WHATWG precedence: the given ``encoding`` (an HTTP
    ``Content-Type`` charset), else an in-document ``<meta charset>`` / BOM, else utf-8 with a
    latin-1 fallback. A leading BOM is stripped and an invalid/unknown charset name degrades
    rather than raising."""
    enc = encoding or sniff_charset(raw)
    if enc:
        try:
            return raw.decode(enc, "replace").lstrip("﻿")
        except LookupError:  # a bogus/unknown charset name -> fall through
            pass
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("latin-1", "replace")
    return text.lstrip("﻿")


def parse_html(data: "str | bytes", *, xml: bool = False) -> Any:
    """Parse HTML/XML into an lxml root element, or ``None`` when lxml is not installed.

    HTML is parsed leniently (a blank body degrades to ``<html></html>`` rather than raising
    lxml's "Document is empty"). XML (``xml=True``) uses the recovering XML parser -- given
    *bytes* so libxml2 honours an in-document ``<?xml encoding?>`` -- so namespaces, tag case
    and CDATA are preserved. Callers that decode first pass a ``str``; the ``tree(core)``
    wrappers cache the result.
    """
    try:
        from lxml import etree, html as _lh
    except ImportError:  # lxml is the optional ``local`` extra -- no tree without it
        return None
    if xml:
        raw = data.encode() if isinstance(data, str) else data
        parsed = etree.fromstring(raw or b"<root/>", parser=etree.XMLParser(recover=True))
        return parsed if parsed is not None else etree.fromstring(b"<root/>")
    text = decode_html(data) if isinstance(data, bytes) else data
    return _lh.fromstring(text if text.strip() else "<html></html>")


def visible_text(html: str) -> str:
    """A rough visible-text extraction from an HTML string WITHOUT lxml: drop script/style
    blocks, strip the remaining tags, and collapse whitespace. Used by the treeless
    (remote / no-lxml) detection paths."""
    return " ".join(_ANY_TAG.sub(" ", _SCRIPT_STYLE.sub(" ", html)).split())


def strip_wc_attrs(html: str) -> str:
    """Remove our internal ``data-wc-*`` correlation-stamp attributes from HTML before it is
    handed back as output (they are never part of the real document)."""
    return _WC_ATTR.sub("", html)


def clean_href(value: "str | None") -> str:
    """Normalise an href/src/action value the way a browser does before resolving it: strip
    leading/trailing ASCII whitespace, drop internal tab/newline/CR, and percent-encode any
    remaining raw spaces -- so a template's newlines or a text-like ``href="Read More"`` don't
    build an un-fetchable URL. ``""`` for an empty value."""
    if not value:
        return ""
    value = value.strip().translate({0x09: None, 0x0A: None, 0x0D: None})
    return value.replace(" ", "%20")
