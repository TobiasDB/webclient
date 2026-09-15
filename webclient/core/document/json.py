"""JsonBacking: dotted-path ops for json documents."""

from __future__ import annotations

import json as _json
import re
from typing import TYPE_CHECKING, Any

from ...collection import Field
from ..web_core import Backing
from .models import Element

if TYPE_CHECKING:
    from . import Document


def _json_elements(value: Any) -> list[Element]:
    out: list[Element] = []

    def walk(v: Any, path: str, parent: str | None) -> None:
        if isinstance(v, dict):
            for k, item in v.items():
                walk(item, f"{path}.{k}" if path else k, path or None)
        elif isinstance(v, list):
            for i, item in enumerate(v):
                walk(item, f"{path}[{i}]", path or None)
        else:
            out.append(Element(id=path, type="text", text=str(v), parent_id=parent))

    walk(value, "", None)
    return out


class JsonBacking(Backing):
    """Dotted-path ops for json. A selected node is a Document holding the
    sub-value; ``attr('value')`` / ``text_content`` read it."""

    provides = frozenset({"select", "select_all", "attr", "render", "elements"})
    collections = frozenset({"select_all"})
    props = frozenset({"text_content"})
    gate = "tree"

    def elements(self, core: "Document") -> "list[Element]":
        """The json as a flat list of typed content blocks (dotted-path ids)."""
        return _json_elements(self._data(core))

    def applies(self, core: "Document") -> bool:
        return core.kind == "json"

    def select_all(
        self,
        core: "Document",
        path: str,
        *,
        limit: int | None = None,
        offset: int = 0,
    ) -> "list[Document]":
        node = self.select(core, path)
        data = None if node._missing else node._element
        items = list(data) if isinstance(data, list) else []
        items = items[offset:]
        if limit is not None:
            items = items[:limit]
        return [core._sub(item) for item in items]

    def render(self, core: "Document", format: str, **options: Any) -> Any:
        if format != "elements":
            raise LookupError(f"no json render format {format!r}")
        return _json_elements(self._data(core))

    def _data(self, core: "Document") -> Any:
        if core._element is not None:
            return core._element  # a selected sub-value
        if core._data is None:
            try:  # a mislabelled / truncated body must not crash a lenient caller
                core._data = _json.loads(core.content or b"null")
            except ValueError:
                core._data = None  # degrade to "no data" (like html's empty tree)
        return core._data

    def select(
        self,
        core: "Document",
        path: str,
        *,
        index: int = 0,  # unified with html select; json paths address nodes directly
        optional: bool = False,
        error: Any = None,
    ) -> "Document":
        from ...errors import RETURN

        from .html import _miss

        value = self._data(core)
        missed = False
        try:
            for tok in re.findall(r"[^.\[\]]+|\[\d+\]", path):
                value = value[int(tok[1:-1])] if tok.startswith("[") else value[tok]
        except (KeyError, IndexError, TypeError):
            missed = True
        if missed:  # a genuine miss: raise/return like html (consistent contract)
            return _miss(core, f"no match for {path!r}", RETURN if optional else error)
        return core._sub(value)

    def attr(
        self, core: "Document", name: str, *, optional: bool = False, error: Any = None
    ) -> Any:
        from ...errors import RAISE, current_policy, select_error

        if core._missing:
            return Field(None, ok=False)
        data = self._data(core)
        if name == "value":  # the node's own value
            return Field(data)
        if isinstance(data, dict) and name in data:
            return Field(data[name])
        # a missing key: same contract as html attr -- raise (structured) by default,
        # a not-ok Field under optional / RETURN. (Never silently return the node.)
        if not optional and (error or current_policy()) is RAISE:
            raise select_error(f"no key {name!r}")
        return Field(None, ok=False)

    def text_content(self, core: "Document") -> "str | None":
        if core._missing:
            return None
        value = self._data(core)
        return value if isinstance(value, str) else _json.dumps(value)


__all__ = ["JsonBacking"]
