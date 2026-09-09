"""Representations. One op, `render(format)`, resolved through the renderer
registry — the same principle as one accessor: a name parameter, not N
methods. A format returns whatever it means: `markdown` a string, `elements`
typed blocks, `links` References."""
from __future__ import annotations

from typing import Any

from ..ops import bind_ops, op
from ..values import Value


@bind_ops
class RenderSurface:
    _backing: Any

    def _renderers(self) -> Any:
        raise NotImplementedError

    @op(pure=True, returns="Value", cardinality="one->one")
    def render(self, format: str, **options: Any) -> Value[Any]:
        """A named representation of this document: `markdown`, `readable`,
        `elements`, `links`, `html`, plus anything registered on the client."""
        registry = self._renderers()
        return Value(registry.render(self, format, **options))
