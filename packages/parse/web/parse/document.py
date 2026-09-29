"""``Document`` and ``Element`` -- the parse layer's output: a resource's CONTENT, interpreted.

A Document is pure content: the raw bytes, the sniffed ``kind`` and text ``encoding``, and the
base ``url`` (only for resolving relative links). It exposes the utilities to find / extract, most
of them thin front doors onto a sibling module (content / structure / records / index / regex /
jsonpath / metadata) so this file stays the interface, not the algorithms. It carries NO transport
facts (status / headers / errors) -- those live on the Snapshot. Parsing is lazy (the lxml tree /
JSON value is built on first use and cached). A selected node is an :class:`Element`, itself
readable and nestable. Ordinary methods returning ordinary values -- laziness is the DSL's job.
"""

from __future__ import annotations

import json as _json
from typing import Literal
from urllib.parse import urljoin

from lxml import etree, html

from . import content as _content
from . import regex as _regex_mod
from . import structure as _structure
from .index import IndexedElement, index_elements
from .jsonpath import JSON, dig, leaves
from .jsonpath import skeleton as _json_skeleton
from .metadata import Metadata, metadata
from .nodes import Node, query
from .nodes import text as _node_text
from .records import RecordRegion, find_records
from .sniff import Kind
from .structure import Heading


class Element:
    """A node selected from a markup :class:`Document` -- readable (``text`` / ``attr``) and
    nestable (``select`` runs against this node's subtree). Wraps one lxml element."""

    def __init__(self, node: Node, base_url: str = "") -> None:
        self._node = node
        self._base = base_url

    @property
    def text(self) -> str:
        """All descendant text, whitespace-collapsed (works for HTML and XML nodes)."""
        return _node_text(self._node)

    @property
    def html(self) -> str:
        """This element serialised back to markup (its own tag included)."""
        return etree.tostring(self._node, encoding="unicode")

    @property
    def inner_html(self) -> str:
        """This element's CHILDREN serialised (its own tag excluded) -- e.g. a ``<body>``'s content
        without the ``<body>`` wrapper, so several can be combined under one root."""
        parts = [self._node.text or ""]
        for child in self._node:
            parts.append(etree.tostring(child, encoding="unicode"))
        return "".join(parts)

    def attr(self, name: str) -> "str | None":
        """An attribute value, or ``None``. ``attr('href')`` / ``attr('src')`` are resolved
        against the document's URL (absolute)."""
        val = self._node.get(name)
        if val is not None and name in ("href", "src") and self._base:
            return urljoin(self._base, val)
        return val

    def select(self, css: str) -> "Element | None":
        """The FIRST descendant matching a CSS selector, or ``None`` (nested selection)."""
        els = query(self._node, css)
        return Element(els[0], self._base) if els else None

    def select_all(self, css: str) -> "list[Element]":
        """ALL descendants matching a CSS selector (nested selection)."""
        return [Element(n, self._base) for n in query(self._node, css)]

    @property
    def region(self) -> str:
        """The page landmark this node sits in -- ``nav`` / ``main`` / ``article`` / ``header`` /
        ``footer`` / ``aside`` (nearest ancestor), or ``""``."""
        return _content.region(self)


class Document:
    """A parsed resource's content. Construct via :func:`web.parse.parse`; read it with
    ``text`` / ``select`` / ``select_all`` / ``links`` / ``json`` per its ``kind``."""

    def __init__(
        self, *, content: bytes, kind: Kind, url: str = "", encoding: str = "utf-8"
    ) -> None:
        self.content = content
        self.kind = kind
        self.url = url  # the base for relative-link resolution only
        self.encoding = encoding
        self._tree: "Node | None" = None
        self._json: JSON = None
        self._json_ready = False

    @property
    def text(self) -> str:
        """The body decoded to text (replacing undecodable bytes)."""
        return self.content.decode(self.encoding, errors="replace")

    def _markup(self) -> bool:
        """Whether this document has a markup tree to select over (html / xml / text)."""
        return self.kind in ("html", "xml", "text")

    def _root(self) -> Node:
        """The lazily-parsed lxml root, cached. Lenient: a malformed document (or empty bytes)
        recovers to as much of a tree as possible, so a read never crashes on bad content.
        """
        tree = self._tree
        if tree is None:
            try:
                if self.kind == "xml":
                    tree = etree.fromstring(self.content, etree.XMLParser(recover=True))
                else:  # html / text: parse leniently as HTML
                    tree = html.fromstring(self.content or b"<html></html>")
            except (etree.ParserError, etree.XMLSyntaxError, ValueError):
                tree = html.fromstring(b"<html></html>")  # unparseable -> empty tree
            self._tree = tree
        return tree

    def select(self, css: str) -> "Element | None":
        """The FIRST element matching a CSS selector, or ``None`` (empty for a non-markup doc)."""
        if not self._markup():
            return None
        els = query(self._root(), css)
        return Element(els[0], self.url) if els else None

    def select_all(self, css: str) -> "list[Element]":
        """ALL elements matching a CSS selector (empty for a non-markup doc)."""
        if not self._markup():
            return []
        return [Element(n, self.url) for n in query(self._root(), css)]

    def links(self) -> "list[str]":
        """Every ``<a href>`` target, resolved absolute against the document URL (empty for a
        non-markup doc)."""
        if not self._markup():
            return []
        return [
            urljoin(self.url, href)
            for a in query(self._root(), "a[href]")
            if (href := a.get("href")) is not None
        ]

    def json(self) -> JSON:
        """The parsed JSON value (JSON documents); cached. Raises on non-JSON."""
        if not self._json_ready:
            self._json = _json.loads(self.content or b"null")
            self._json_ready = True
        return self._json

    # -- content extraction (thin front doors onto the sibling modules) --

    def main_content(self) -> "Element | None":
        """The page's main content region (``<main>``/``<article>``/densest block), or ``None``."""
        return _content.main_content(self)

    def readable(self, *, main_content_only: bool = True) -> str:
        """The page's readable text, chrome stripped (nav/footer/scripts), whitespace collapsed."""
        return _content.readable_text(self, main_content_only=main_content_only)

    def markdown(self, *, main_content_only: bool = False) -> str:
        """The page rendered as markdown -- headings, links, lists, emphasis, code."""
        return _content.markdown(self, main_content_only=main_content_only)

    def tables(
        self, selector: "str | None" = None, *, transpose: bool = False
    ) -> "list[dict[str, str]]":
        """HTML ``<table>`` rows as header-keyed records, with rowspan/colspan expanded."""
        return _content.tables(self, selector, transpose=transpose)

    def regex(
        self, pattern: str, *, group: "int | str" = 0, flags: int = 0
    ) -> "str | None":
        """The first ``pattern`` match in the document text (``group`` of it), or ``None``."""
        return _regex_mod.regex(self, pattern, group=group, flags=flags)

    def regex_all(
        self, pattern: str, *, group: "int | str" = 0, flags: int = 0
    ) -> "list[str]":
        """Every ``pattern`` match in the document text, each reduced to ``group``."""
        return _regex_mod.regex_all(self, pattern, group=group, flags=flags)

    def skeleton(
        self,
        *,
        max_lines: int = 400,
        text_chars: int = 40,
        max_depth: int = 30,
        mark_records: bool = True,
        mark_interactive: bool = True,
        drop_chrome: bool = False,
    ) -> str:
        """A token-lean indented open-tag outline of the DOM (for cheap selector authoring), with the
        record list + non-obvious controls flagged in place (see :mod:`.structure`)."""
        return _structure.skeleton(
            self,
            max_lines=max_lines,
            text_chars=text_chars,
            max_depth=max_depth,
            mark_records=mark_records,
            mark_interactive=mark_interactive,
            drop_chrome=drop_chrome,
        )

    def outline(self) -> "list[Heading]":
        """The document's heading tree (``<h1>``..``<h6>``) in order."""
        return _structure.outline(self)

    def metadata(self) -> Metadata:
        """Head-level facts: title, description, canonical, OpenGraph, JSON-LD, feeds."""
        return metadata(self)

    def records(self, *, min_items: int = 3, top_k: int = 3) -> "list[RecordRegion]":
        """The dominant repeating regions (the dataset) with a suggested ``select_all`` selector --
        the mechanical answer to "where is the list?" (see :mod:`.records`)."""
        return find_records(self, min_items=min_items, top_k=top_k)

    def index(
        self,
        *,
        kind: "Literal['interactive', 'content']" = "interactive",
        limit: int = 200,
    ) -> "list[IndexedElement]":
        """The numbered element table -- controls to drive (``interactive``) or text leaves to
        extract (``content``), each with a durable class-free selector (see :mod:`.index`).
        """
        return index_elements(self, kind=kind, limit=limit)

    # -- JSON navigation (JSON documents; see :mod:`.jsonpath`) --
    # These are SAFE on a non-JSON document (they no-op), mirroring how the markup reads are safe
    # on a JSON one; use ``json()`` directly when you want the raise-on-non-JSON behaviour.

    def at(self, path: str) -> JSON:
        """Follow a dotted path into the parsed JSON (``"data.results[0].name"``), or ``None`` (also
        ``None`` for a non-JSON document)."""
        return dig(self.json(), path) if self.kind == "json" else None

    def json_skeleton(self, *, max_lines: int = 400, text_chars: int = 40) -> str:
        """A token-lean outline of the JSON shape (``""`` for a non-JSON document)."""
        if self.kind != "json":
            return ""
        return _json_skeleton(self.json(), max_lines=max_lines, text_chars=text_chars)

    def json_leaves(self, *, budget: int = 20000) -> "list[str]":
        """Every scalar leaf of the JSON value, as strings (empty for a non-JSON document)."""
        if self.kind != "json":
            return []
        return leaves(self.json(), budget=budget)


__all__ = ["Document", "Element"]
