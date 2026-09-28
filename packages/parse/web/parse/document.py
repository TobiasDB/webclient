"""``Document`` and ``Element`` -- the parse layer's output: a resource interpreted.

A Document is plain code: it holds the raw bytes plus what fetch reported (url, status,
headers), knows its sniffed ``kind`` and text ``encoding``, and exposes reads over the parsed
content -- ``text``, ``select(css)`` / ``links()`` for markup, ``json()`` for JSON. Parsing is
lazy (the lxml tree / JSON value is built on first use and cached), so a Document is cheap to
construct. A selected node is an :class:`Element`, itself readable and nestable -- ``select``
composes.

No laziness-as-a-plan, no dispatch: these are ordinary methods returning ordinary values. A
sync/lazy/remote/recording face is the DSL layer's job, built on top of these plain reads.
"""

from __future__ import annotations

import json as _json
from typing import Any
from urllib.parse import urljoin

from web.kernel import WebError

from .sniff import Kind


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

    def select(self, css: str) -> "list[Element]":
        """Descendant elements matching a CSS selector (nested selection)."""
        return [Element(n, self._base) for n in self._node.cssselect(css)]


class Document:
    """A parsed resource. Construct via :func:`web.parse.parse` / :func:`parse_bytes`; read it
    with ``text`` / ``select`` / ``links`` / ``json`` per its ``kind``."""

    def __init__(
        self,
        *,
        content: bytes,
        kind: Kind,
        url: str = "",
        status: int = 0,
        headers: dict[str, str] | None = None,
        encoding: str = "utf-8",
        error: WebError | None = None,
    ) -> None:
        self.content = content
        self.kind = kind
        self.url = url
        self.status = status
        self.headers = headers or {}
        self.encoding = encoding
        self.error = error
        self._tree: Any = None
        self._json: Any = _UNSET

    @property
    def ok(self) -> bool:
        """No parse/transport error and, when a status is known, a 2xx one."""
        return self.error is None and (self.status == 0 or 200 <= self.status < 300)

    @property
    def text(self) -> str:
        """The body decoded to text (replacing undecodable bytes)."""
        return self.content.decode(self.encoding, errors="replace")

    def _root(self) -> Any:
        """The lazily-parsed lxml root (html or xml), cached."""
        if self._tree is None:
            if self.kind == "json":
                raise TypeError("select/links are for markup documents, not JSON")
            from lxml import etree, html

            if self.kind == "xml":
                self._tree = etree.fromstring(self.content)
            else:  # html / text: parse leniently as HTML
                self._tree = html.fromstring(self.content or b"<html></html>")
        return self._tree

    def select(self, css: str) -> "list[Element]":
        """Elements matching a CSS selector (markup documents)."""
        return [Element(n, self.url) for n in self._root().cssselect(css)]

    def links(self) -> "list[str]":
        """Every ``<a href>`` target, resolved absolute against the document URL."""
        out: list[str] = []
        for a in self._root().cssselect("a[href]"):
            out.append(urljoin(self.url, a.get("href")))
        return out

    def json(self) -> Any:
        """The parsed JSON value (JSON documents); cached. Raises on non-JSON."""
        if self._json is _UNSET:
            self._json = _json.loads(self.content or b"null")
        return self._json


class _Unset:
    __slots__ = ()


_UNSET = _Unset()


__all__ = ["Document", "Element"]
