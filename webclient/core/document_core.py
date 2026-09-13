"""DocumentCore: the core behind a document (MVP).

Core Fields = the resolved response (the surface's data). Backings = per-medium
op providers: HtmlBacking (css/xpath select, attr, text) and JsonBacking (dotted
path). A selected element is itself a DocumentCore (subtree / json sub-value),
so selection nests. Render / live / events are later slices.
"""
from __future__ import annotations

import json as _json
from typing import TYPE_CHECKING, Any, ClassVar, Literal
from urllib.parse import urljoin

from pydantic import BaseModel, PrivateAttr

from .reference_core import ReferenceCore, from_url
from .web_core import Backing, WebCore

if TYPE_CHECKING:
    from .client_core import WebClientCore


def _norm(text: str) -> str:
    return " ".join(text.split())


class HtmlBacking(Backing):
    """Tree ops for html/xml. ``select``/``select_all`` yield element
    DocumentCores; ``attr``/``text`` read from the element (or body)."""

    provides = frozenset({"select", "select_all", "attr"})
    props = frozenset({"text"})
    gate = "tree"

    def applies(self, core: "DocumentCore") -> bool:
        return core.kind in ("html", "xml")

    def _tree(self, core: "DocumentCore") -> Any:
        if core._element is not None:
            return core._element
        if core._tree is None:
            from lxml import html as _lh
            core._tree = _lh.fromstring(core.content or b"<html></html>")
        return core._tree

    def _find(self, core: "DocumentCore", selector: str) -> list[Any]:
        root = self._tree(core)
        if selector.startswith("/") or selector.startswith("./"):
            return list(root.xpath(selector))
        return list(root.cssselect(selector))

    def select(self, core: "DocumentCore", selector: str, *,
               index: int = 0) -> "DocumentCore":
        els = self._find(core, selector)
        return _element(core, els[index] if len(els) > index else None)

    def select_all(self, core: "DocumentCore", selector: str,
                   ) -> "list[DocumentCore]":
        return [_element(core, el) for el in self._find(core, selector)]

    def attr(self, core: "DocumentCore", name: str) -> "str | ReferenceCore":
        el = core._element
        if name == "text":
            return self.text(core)
        value = el.get(name, "") if el is not None else ""
        if name in ("href", "src", "action"):
            ref = from_url(urljoin(core.final_url or core.url, value))
            ref._client = core._client               # inherit the client so it resolves
            return ref
        return value

    def text(self, core: "DocumentCore") -> str:
        el = core._element if core._element is not None else self._tree(core)
        return _norm("".join(el.itertext()))


class JsonBacking(Backing):
    """Dotted-path ops for json. A selected node is a DocumentCore holding the
    sub-value; ``attr('value')`` / ``text`` read it."""

    provides = frozenset({"select", "attr"})
    props = frozenset({"text"})
    gate = "tree"

    def applies(self, core: "DocumentCore") -> bool:
        return core.kind == "json"

    def _data(self, core: "DocumentCore") -> Any:
        if core._element is not None:
            return core._element                      # a selected sub-value
        if core._data is None:
            core._data = _json.loads(core.content or b"null")
        return core._data

    def select(self, core: "DocumentCore", path: str) -> "DocumentCore":
        import re
        value = self._data(core)
        try:
            for tok in re.findall(r"[^.\[\]]+|\[\d+\]", path):
                value = value[int(tok[1:-1])] if tok.startswith("[") else value[tok]
        except (KeyError, IndexError, TypeError):
            value = None
        return _element(core, value)

    def attr(self, core: "DocumentCore", name: str) -> Any:
        return self._data(core)

    def text(self, core: "DocumentCore") -> str:
        value = self._data(core)
        return value if isinstance(value, str) else _json.dumps(value)


def _element(parent: "DocumentCore", node: Any) -> "DocumentCore":
    """A selected element/value as a DocumentCore rooted at ``parent``."""
    sub = DocumentCore(url=parent.url, final_url=parent.final_url,
                       kind=parent.kind, status_code=parent.status_code)
    sub._client = parent._client
    sub._element = node
    return sub


class DocumentCore(WebCore, BaseModel):
    """A resolved resource's core (+ element sub-cores). Core Fields are the
    response; behaviour is the backings."""

    id: str = ""
    kind: Literal["html", "json", "xml", "binary"] = "html"
    url: str = ""
    final_url: str | None = None
    content: bytes = b""
    status_code: int = 0
    response_headers: dict[str, str] = {}
    encoding: str | None = None

    _client: Any = PrivateAttr(default=None)      # owning WebClientCore
    _element: Any = PrivateAttr(default=None)     # lxml element / json sub-value
    _tree: Any = PrivateAttr(default=None)        # cached lxml parse
    _data: Any = PrivateAttr(default=None)        # cached json

    BACKINGS: ClassVar[tuple[Backing, ...]] = (HtmlBacking(), JsonBacking())

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300 or self.status_code == 0


__all__ = ["DocumentCore", "HtmlBacking", "JsonBacking"]
