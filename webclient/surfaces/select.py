"""Selection: the surface every backing must serve.

Written once against the backing's address-based primitives, so the same ops
run over an immutable lxml tree and over a live page. Requires `_backing`,
`_path` (None means the document root) and `_spawn_element` from its host.
"""
from __future__ import annotations

from typing import Any, Literal, Sequence, overload

from .._async import chain
from ..backing import _LINK_ATTRS
from ..errors import SelectionError
from ..ops import bind_ops, op
from ..reference import Reference
from ..values import Selection, Value


@bind_ops
class SelectSurface:
    """`select` / `select_all` / `attr` — CSS or XPath, elements only."""

    _backing: Any
    _path: str | None

    def _spawn_element(self, path: str) -> Any:
        raise NotImplementedError

    def _link_base(self) -> Any:
        raise NotImplementedError

    # -- ops -----------------------------------------------------------------
    @op(pure=True, returns="Element", cardinality="one->one")
    def select(self, selector: str, *, index: int = 0,
               optional: bool = False) -> Any:
        """One element by CSS or XPath. Raises `SelectionError` when there is
        no match unless `optional=True`."""
        if index < 0:
            found = self._backing.select_paths(self._path, selector)
        else:
            found = self._backing.select_paths(self._path, selector,
                                               limit=1, offset=index)

        def pick(paths: Sequence[str]) -> Any:
            if index < 0:
                paths = paths[index:index + 1] if -index <= len(paths) else []
            if not paths:
                if optional:
                    return None
                raise SelectionError(
                    f"no match for {selector!r} at index {index}")
            return self._spawn_element(paths[0])

        return chain(found, pick)

    @op(pure=True, returns="Selection", cardinality="one->many")
    def select_all(self, selector: str, *, limit: int | None = None,
                   offset: int = 0) -> Selection[Any]:
        """Every match, in document order."""
        found = self._backing.select_paths(self._path, selector,
                                           limit=limit, offset=offset)
        return chain(found, lambda paths: Selection(
            [self._spawn_element(p) for p in paths]))

    @overload  # link attrs deliberately narrow str -> Reference
    def attr(self, name: Literal["href", "src", "action"], *,  # type: ignore[overload-overlap]
             optional: bool = ...) -> "Reference": ...
    @overload
    def attr(self, name: str, *, optional: bool = ...) -> Value[str]: ...

    @op(pure=True, returns="Value", cardinality="one->one",
        returns_for=lambda args, kwargs: (
            "Reference" if (args and args[0] in _LINK_ATTRS) else "Value"))
    def attr(self, name: str, *, optional: bool = False) -> Any:
        """The one accessor.

        Covers real attributes and the pseudo-attributes `text`, `html`,
        `body` and (on a document) `title`. Link attributes narrow to a
        `Reference` — resolved against the URL that actually answered, not
        the one that was requested — so `.attr("href").resolve()` is a
        complete thought in either evaluation mode.
        """
        raw = self._backing.attr_at(self._path, name)

        def wrap(value: Any) -> Any:
            if value is None:
                if optional:
                    return Value(None)
                raise SelectionError(
                    f"no attribute {name!r} on {self._describe()}")
            if name in _LINK_ATTRS:
                return self._link_base().join(value)
            return Value(value)

        return chain(raw, wrap)

    def _describe(self) -> str:
        return "this element" if self._path else "this document"
