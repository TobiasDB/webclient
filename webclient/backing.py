"""Backings: the single owner of a Document's mutable resource.

A Document holds a Backing and nothing else from the client. The backing
decides what the Document can *do* (`capabilities`) and provides the
address-based primitives every surface is written against, so the same
`select` op works over an immutable lxml tree and over a live page.

Element addresses are strings with a scheme prefix:

    xpath:/html/body/div[2]     tree backings — exact, stable, cheap
    nid:n1/n4/n9                page backings — injected stable node ids

They are opaque to callers and serialisable, which is what lets an Element
cross a plan boundary.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Literal, Sequence

from .errors import SelectionError

Kind = Literal["html", "json", "xml", "binary"]

#: capability names ops declare in `@op(capability=…)`
TREE = "tree"
BROWSER = "browser"

_PSEUDO_ATTRS = ("text", "html", "title", "body")
_LINK_ATTRS = ("href", "src", "action")


def sniff_kind(content_type: str | None, content: bytes) -> Kind:
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


def is_xpath(selector: str) -> bool:
    return selector.startswith("/") or selector.startswith("./")


# --------------------------------------------------------------------------- #
# Base
# --------------------------------------------------------------------------- #

class Backing(ABC):
    """What a Document is backed by."""

    name: str = "none"
    capabilities: frozenset[str] = frozenset()

    kind: Kind = "html"
    content: bytes = b""
    encoding: str | None = None
    status_code: int = 0
    final_url: str | None = None
    response_headers: dict[str, str]
    elapsed: float | None = None
    message: str | None = None

    def __init__(self) -> None:
        self.response_headers = {}

    # -- selection primitives (sync here; page backings return coroutines) ---
    @abstractmethod
    def select_paths(self, root: str | None, selector: str, *,
                     limit: int | None = None, offset: int = 0) -> Any: ...

    @abstractmethod
    def count_paths(self, root: str | None, selector: str) -> Any: ...

    @abstractmethod
    def attr_at(self, path: str | None, name: str) -> Any: ...

    async def aclose(self) -> None:
        """Release whatever this backing holds. Idempotent."""


# --------------------------------------------------------------------------- #
# Tree backings (lxml)
# --------------------------------------------------------------------------- #

class TreeBacking(Backing):
    """An immutable parsed document. Addresses are lxml element paths, which
    are exact and stable precisely because the tree never changes."""

    name = "static"
    capabilities = frozenset({TREE})

    def __init__(self, content: bytes = b"", kind: Kind = "html", *,
                 encoding: str | None = None, status_code: int = 200,
                 final_url: str | None = None,
                 response_headers: dict[str, str] | None = None,
                 elapsed: float | None = None,
                 message: str | None = None) -> None:
        super().__init__()
        self.content = content
        self.kind = kind
        self.encoding = encoding
        self.status_code = status_code
        self.final_url = final_url
        self.response_headers = response_headers or {}
        self.elapsed = elapsed
        self.message = message
        self._tree: Any = None

    # -- parsing -------------------------------------------------------------
    @property
    def text(self) -> str:
        if not self.content:
            return ""
        if self.encoding:
            try:
                return self.content.decode(self.encoding)
            except (LookupError, UnicodeDecodeError):
                pass
        try:
            from charset_normalizer import from_bytes
            best = from_bytes(self.content).best()
            if best is not None:
                return str(best)
        except ImportError:
            pass
        return self.content.decode("utf-8", errors="replace")

    def tree(self) -> Any:
        if self._tree is None:
            from lxml import etree, html as lxml_html
            if self.kind == "html":
                self._tree = lxml_html.fromstring(
                    self.content or b"<html></html>")
            elif self.kind == "xml":
                if not self.content:
                    raise SelectionError("cannot parse an empty XML document")
                self._tree = etree.fromstring(
                    self.content, parser=etree.XMLParser(recover=True))
            else:
                raise SelectionError(
                    f"select() needs a parsed tree; this document is "
                    f"{self.kind!r}")
        return self._tree

    def _root_tree(self) -> Any:
        return self.tree().getroottree()

    def element_at(self, path: str | None) -> Any:
        if path is None:
            return self.tree()
        scheme, _, value = path.partition(":")
        if scheme != "xpath":
            raise SelectionError(f"not a tree address: {path!r}")
        found = self._root_tree().xpath(value)
        if not found:
            raise SelectionError(
                f"element address {path!r} no longer resolves")
        return found[0]

    def address_of(self, element: Any) -> str:
        return "xpath:" + self._root_tree().getpath(element)

    # -- primitives ----------------------------------------------------------
    def _matches(self, root: str | None, selector: str) -> list[Any]:
        from lxml import etree
        node = self.element_at(root)
        if is_xpath(selector):
            results = node.xpath(selector)
            if not isinstance(results, list):
                raise SelectionError(
                    f"XPath {selector!r} produces a scalar; selection returns "
                    "elements only — use .attr() for values")
            if any(not isinstance(r, etree._Element) for r in results):
                raise SelectionError(
                    f"XPath {selector!r} selects non-elements; selection "
                    "returns elements only — use .attr() for values")
            return results
        return node.cssselect(selector)

    def select_paths(self, root: str | None, selector: str, *,
                     limit: int | None = None, offset: int = 0) -> list[str]:
        matches = self._matches(root, selector)
        end = None if limit is None else offset + limit
        return [self.address_of(e) for e in matches[offset:end]]

    def count_paths(self, root: str | None, selector: str) -> int:
        return len(self._matches(root, selector))

    def attr_at(self, path: str | None, name: str) -> Any:
        if name == "body" and path is None:
            return self.text            # decoded response body, no tree
        element = self.element_at(path)
        if name == "text":
            return " ".join("".join(element.itertext()).split())
        if name == "html":
            from lxml import etree
            return etree.tostring(element, encoding="unicode")
        if name == "title" and path is None:
            found = self._matches(None, "title")
            if not found:
                return None
            return " ".join("".join(found[0].itertext()).split())
        return element.get(name)


class StaticBacking(TreeBacking):
    """A document built from content the caller already had."""

    name = "static"


class HttpBacking(TreeBacking):
    """A document produced by an HTTP response. Same primitives as any tree
    backing; it exists to carry provenance and telemetry."""

    name = "http"

    def __init__(self, *args: Any, telemetry: Any = None,
                 request: Any = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.telemetry = telemetry
        self.request = request
