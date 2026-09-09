"""Representations, as a registry of functions.

`(kind, format) -> fn(document, **options)`. No plugin base class, no
`surfaces=[...]`, no attach/detach lifecycle — the one extension point most
callers ever touch is a dict you can put a function in.
"""
from __future__ import annotations

from typing import Any, Callable, Iterator

from pydantic import BaseModel

Renderer = Callable[..., Any]


class Block(BaseModel):
    """A typed content block — the `elements` representation."""

    id: str = ""
    type: str = "text"              # title/text/list_item/table/code/image
    text: str = ""
    parent_id: str | None = None
    metadata: dict[str, Any] = {}


class RendererRegistry:
    """Keyed on `(kind, format)`; `"*"` matches any kind."""

    def __init__(self, parent: "RendererRegistry | None" = None) -> None:
        self._table: dict[tuple[str, str], Renderer] = {}
        self._parent = parent

    def register(self, kind: str, format: str, fn: Renderer) -> Renderer:
        self._table[(kind, format)] = fn
        return fn

    def lookup(self, kind: str, format: str) -> Renderer | None:
        found = self._table.get((kind, format)) or self._table.get(("*", format))
        if found is None and self._parent is not None:
            return self._parent.lookup(kind, format)
        return found

    def formats(self, kind: str | None = None) -> list[str]:
        names = {fmt for (k, fmt) in self._table if kind in (None, k, "*")}
        if self._parent is not None:
            names.update(self._parent.formats(kind))
        return sorted(names)

    def render(self, document: Any, format: str, **options: Any) -> Any:
        kind = document._backing.kind
        fn = self.lookup(kind, format)
        if fn is None:
            raise LookupError(
                f"no renderer for kind={kind!r} format={format!r}; "
                f"registered: {', '.join(self.formats(kind)) or 'none'}")
        return fn(document, **options)

    def child(self) -> "RendererRegistry":
        return RendererRegistry(parent=self)


_default: RendererRegistry | None = None


def default_registry() -> RendererRegistry:
    global _default
    if _default is None:
        _default = RendererRegistry()
        from . import html as _html, structured as _structured
        _html.install(_default)
        _structured.install(_default)
    return _default
