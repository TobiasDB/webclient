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

    provides = frozenset({"select", "select_all", "attr", "render"})
    props = frozenset({"text_content"})
    gate = "tree"

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
            core._data = _json.loads(core.content or b"null")
        return core._data

    def select(self, core: "Document", path: str) -> "Document":
        value = self._data(core)
        try:
            for tok in re.findall(r"[^.\[\]]+|\[\d+\]", path):
                value = value[int(tok[1:-1])] if tok.startswith("[") else value[tok]
        except (KeyError, IndexError, TypeError):
            value = None
        return core._sub(value)

    def attr(self, core: "Document", name: str, *, error: Any = None) -> Any:
        if core._missing:
            return Field(None, ok=False)
        data = self._data(core)
        if name != "value" and isinstance(data, dict) and name in data:
            return Field(data[name])
        return Field(data)

    def text_content(self, core: "Document") -> "str | None":
        if core._missing:
            return None
        value = self._data(core)
        return value if isinstance(value, str) else _json.dumps(value)


__all__ = ["JsonBacking"]
