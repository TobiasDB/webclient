"""``Document`` and ``Element`` -- the parse layer's output: a resource's CONTENT, interpreted.

A Document is pure content: the raw bytes, the sniffed ``kind`` and text ``encoding``, and the
base ``url`` (only for resolving relative links). It exposes the utilities to find / extract --
``text``, ``select`` / ``select_all`` / ``links`` for markup, ``json`` for JSON. It carries NO
transport facts (status / headers / errors) -- those live on the Snapshot and are used internally
by resolve; a Document is just what the bytes say. Parsing is lazy (the lxml tree / JSON value is
built on first use and cached). A selected node is an :class:`Element`, itself readable and
nestable -- ``select`` composes. Ordinary methods returning ordinary values (no dispatch/laziness
-- that is the DSL's job on top).
"""

from __future__ import annotations

import json as _json
from typing import TYPE_CHECKING, Any
from urllib.parse import urljoin

from .sniff import Kind

if TYPE_CHECKING:
    from .metadata import Metadata
    from .structure import Heading


class Element:
    """A node selected from a markup :class:`Document` -- readable (``text`` / ``attr``) and
    nestable (``select`` runs against this node's subtree). Wraps one lxml element."""

    def __init__(self, node: Any, base_url: str = "") -> None:
        self._node = node
        self._base = base_url

    @property
    def text(self) -> str:
        """All descendant text, whitespace-collapsed."""
        return " ".join(self._node.text_content().split())

    @property
    def html(self) -> str:
        """This element serialised back to markup."""
        from lxml import etree

        return etree.tostring(self._node, encoding="unicode")

    def attr(self, name: str) -> str | None:
        """An attribute value, or ``None``. ``attr('href')`` / ``attr('src')`` are resolved
        against the document's URL (absolute)."""
        val: str | None = self._node.get(name)
        if val is not None and name in ("href", "src") and self._base:
            return urljoin(self._base, val)
        return val

    def select(self, css: str) -> "Element | None":
        """The FIRST descendant matching a CSS selector, or ``None`` (nested selection)."""
        els = self._node.cssselect(css)
        return Element(els[0], self._base) if els else None

    def select_all(self, css: str) -> "list[Element]":
        """ALL descendants matching a CSS selector (nested selection)."""
        return [Element(n, self._base) for n in self._node.cssselect(css)]

    @property
    def region(self) -> str:
        """The page landmark this node sits in -- ``nav`` / ``main`` / ``article`` / ``header`` /
        ``footer`` / ``aside`` (nearest ancestor), or ``""``."""
        from .content import region

        return region(self)


class Document:
    """A parsed resource's content. Construct via :func:`web.parse.parse`; read it with
    ``text`` / ``select`` / ``select_all`` / ``links`` / ``json`` per its ``kind``."""

    def __init__(self, *, content: bytes, kind: Kind, url: str = "", encoding: str = "utf-8") -> None:
        self.content = content
        self.kind = kind
        self.url = url  # the base for relative-link resolution only
        self.encoding = encoding
        self._tree: Any = None
        self._json: Any = _UNSET

    @property
    def text(self) -> str:
        """The body decoded to text (replacing undecodable bytes)."""
        return self.content.decode(self.encoding, errors="replace")

    def _markup(self) -> bool:
        """Whether this document has a markup tree to select over (html / xml / text)."""
        return self.kind in ("html", "xml", "text")

    def _root(self) -> Any:
        """The lazily-parsed lxml root, cached. Lenient: a malformed document (or empty bytes)
        recovers to as much of a tree as possible, so a read never crashes on bad content."""
        if self._tree is None:
            from lxml import etree, html

            try:
                if self.kind == "xml":
                    self._tree = etree.fromstring(self.content, etree.XMLParser(recover=True))
                else:  # html / text: parse leniently as HTML
                    self._tree = html.fromstring(self.content or b"<html></html>")
            except (etree.ParserError, etree.XMLSyntaxError, ValueError):
                self._tree = html.fromstring(b"<html></html>")  # unparseable -> empty tree
        return self._tree

    def select(self, css: str) -> "Element | None":
        """The FIRST element matching a CSS selector, or ``None`` (empty for a non-markup doc)."""
        if not self._markup():
            return None
        els = self._root().cssselect(css)
        return Element(els[0], self.url) if els else None

    def select_all(self, css: str) -> "list[Element]":
        """ALL elements matching a CSS selector (empty for a non-markup doc)."""
        if not self._markup():
            return []
        return [Element(n, self.url) for n in self._root().cssselect(css)]

    def links(self) -> "list[str]":
        """Every ``<a href>`` target, resolved absolute against the document URL (empty for a
        non-markup doc)."""
        if not self._markup():
            return []
        return [urljoin(self.url, a.get("href")) for a in self._root().cssselect("a[href]")]

    def json(self) -> Any:
        """The parsed JSON value (JSON documents); cached. Raises on non-JSON."""
        if self._json is _UNSET:
            self._json = _json.loads(self.content or b"null")
        return self._json

    # -- content extraction (implemented in sibling modules to keep this file lean) --

    def main_content(self) -> "Element | None":
        """The page's main content region (``<main>``/``<article>``/densest block), or ``None``."""
        from .content import main_content

        return main_content(self)

    def readable(self, *, main_content_only: bool = True) -> str:
        """The page's readable text, chrome stripped (nav/footer/scripts), whitespace collapsed."""
        from .content import readable_text

        return readable_text(self, main_content_only=main_content_only)

    def markdown(self, *, main_content_only: bool = False) -> str:
        """The page rendered as markdown -- headings, links, lists, emphasis, code."""
        from .content import markdown

        return markdown(self, main_content_only=main_content_only)

    def tables(self, selector: "str | None" = None, *, transpose: bool = False) -> "list[dict[str, str]]":
        """HTML ``<table>`` rows as header-keyed records, with rowspan/colspan expanded."""
        from .content import tables

        return tables(self, selector, transpose=transpose)

    def regex(self, pattern: str, *, group: "int | str" = 0, flags: int = 0) -> "str | None":
        """The first ``pattern`` match in the document text (``group`` of it), or ``None``."""
        from .regex import regex

        return regex(self, pattern, group=group, flags=flags)

    def regex_all(self, pattern: str, *, group: "int | str" = 0, flags: int = 0) -> "list[str]":
        """Every ``pattern`` match in the document text, each reduced to ``group``."""
        from .regex import regex_all

        return regex_all(self, pattern, group=group, flags=flags)

    def skeleton(self, *, max_lines: int = 400, text_chars: int = 40, max_depth: int = 30) -> str:
        """A token-lean indented open-tag outline of the DOM (for cheap selector authoring)."""
        from .structure import skeleton

        return skeleton(self, max_lines=max_lines, text_chars=text_chars, max_depth=max_depth)

    def outline(self) -> "list[Heading]":
        """The document's heading tree (``<h1>``..``<h6>``) in order."""
        from .structure import outline

        return outline(self)

    def metadata(self) -> "Metadata":
        """Head-level facts: title, description, canonical, OpenGraph, JSON-LD, feeds."""
        from .metadata import metadata

        return metadata(self)


class _Unset:
    __slots__ = ()


_UNSET = _Unset()


__all__ = ["Document", "Element"]
