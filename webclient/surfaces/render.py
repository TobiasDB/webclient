"""Representations. One op, `render(format)`, resolved through the renderer
registry — the same principle as one accessor: a name parameter, not N
methods. `links()` is not a rendering: it extracts References, so it is its
own op."""
from __future__ import annotations

from typing import Any

from ..ops import bind_ops, op
from ..values import Selection, Value


@bind_ops
class RenderSurface:
    _backing: Any

    def _renderers(self) -> Any:
        raise NotImplementedError

    @op(pure=True, returns="Value", cardinality="one->one")
    def render(self, format: str, **options: Any) -> Value[Any]:
        """A named representation of this document: `markdown`, `readable`,
        `elements`, `html`, plus anything registered on the client."""
        registry = self._renderers()
        return Value(registry.render(self, format, **options))

    @op(pure=True, returns="Selection", cardinality="one->many")
    def links(self, selector: str = "a[href]") -> Selection[Any]:
        """Every link as a `Reference`, resolved against the URL that
        answered."""
        from .._async import chain
        found = self._backing.select_paths(None, selector)

        def build(paths: list[str]) -> Selection[Any]:
            base = self._link_base()               # type: ignore[attr-defined]
            out = []
            for path in paths:
                for name in ("href", "src", "action"):
                    value = self._backing.attr_at(path, name)
                    if value:
                        out.append(base.join(value))
                        break
            return Selection(out)

        return chain(found, build)
