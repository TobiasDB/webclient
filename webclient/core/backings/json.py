"""JSON backing: dotted-path selection, attributes, and the elements render.
"""
from __future__ import annotations

import json as _json
from typing import TYPE_CHECKING, Any, ClassVar

from ..models import Collection, Document, Element, Field, Reference, _json_path
from ..base import Capability
from .base import Backing

if TYPE_CHECKING:
    from ..document import DocumentCore


class JsonBacking(Backing):
    provides = frozenset({"select", "select_all", "attr", "render"})
    gate: ClassVar[Capability] = "tree"

    def _value(self, core: "DocumentCore") -> Any:
        return core.data if core.is_element else core.json()

    def select(self, core: "DocumentCore", selector: str, *, index: int = 0,
               wait: float | None = None) -> Document:
        matches = _json_matches(self._value(core), selector)
        try:
            return core.element(data=matches[index])
        except IndexError:
            raise LookupError(
                f"no match for {selector!r} at index {index}") from None

    def select_all(self, core: "DocumentCore", selector: str, limit: int | None = None,
                   offset: int = 0) -> Collection[Document]:
        matches = _json_matches(self._value(core), selector)
        matches = matches[offset:offset + limit if limit is not None else None]
        out: Collection[Document] = Collection()
        out._items = [core.element(data=m) for m in matches]
        return out

    def attr(self, core: "DocumentCore", name: str) -> Field[str] | Reference:
        data = self._value(core)
        value = data if name in ("value", "text") else _json_path(data, name)
        return Field[str](value=value)


    def render(self, core: "DocumentCore", format: str, **options: Any) -> Any:
        client = core.client
        if client is not None:
            override = client._render_table.get((core.doc.kind, format))
            if override is not None:
                return override.render(core.doc, format, **options)
        if format != "elements":
            raise LookupError(f"no json render format {format!r}")
        out: list[Element] = []

        def walk(value: Any, path: str, parent: str | None) -> None:
            if isinstance(value, dict):
                for key, item in value.items():
                    walk(item, f"{path}.{key}" if path else key, path or None)
            elif isinstance(value, list):
                for i, item in enumerate(value):
                    walk(item, f"{path}[{i}]", path or None)
            else:
                out.append(Element(id=path, type="text", text=str(value),
                                   parent_id=parent))

        walk(self._value(core), "", None)
        return out


def _json_matches(value: Any, selector: str) -> list[Any]:
    got = _json_path(value, selector)
    return got if isinstance(got, list) else [got]


# --------------------------------------------------------------------------- #
# Live page (playwright): selection + actions
# --------------------------------------------------------------------------- #
