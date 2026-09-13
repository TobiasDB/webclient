"""Collection and Field: the two surface types that carry a little behaviour.

Every other surface is a method-free wrapper over a core (``webclient.surface``),
but a ``Collection`` (a fanned-out set of results) and a ``Field`` (a scalar
leaf) are the sanctioned exceptions -- they hold minimal built-in helpers.
``Collection`` runs the row-shaping ops (``extract`` / ``filter`` / ``project``
/ ``documents``) by evaluating sub-expressions against each element; ``Field``
is the value leaf (``get`` + ``is_ok``/``is_empty`` + comparisons + truthiness).
"""
from __future__ import annotations

from typing import Any, Iterator


class Field:
    """A scalar leaf: a value plus whether it is present/ok."""

    __slots__ = ("_value", "_ok")

    def __init__(self, value: Any = None, *, ok: bool = True) -> None:
        self._value = value
        self._ok = ok and value is not None

    def get(self, default: Any = None) -> Any:
        return self._value if self._ok else default

    @property
    def value(self) -> Any:
        return self._value

    @property
    def ok(self) -> bool:
        return self._ok

    def is_ok(self) -> "Field":
        return Field(self._ok)

    def is_empty(self) -> "Field":
        return Field(not self._ok or self._value in ("", [], {}, None))

    def __bool__(self) -> bool:
        return bool(self._value) if self._ok else False

    def __eq__(self, o: Any) -> bool:   # type: ignore[override]
        return self.get() == (o.get() if isinstance(o, Field) else o)

    def __ne__(self, o: Any) -> bool:   # type: ignore[override]
        return not self.__eq__(o)

    def __hash__(self) -> int:
        return hash(self._value) if self._ok else 0

    def __repr__(self) -> str:
        return f"Field({self._value!r})" if self._ok else "Field(<empty>)"


def _raw(value: Any) -> Any:
    """Unwrap a Field to its raw value (missing -> None); pass anything else."""
    return value.get() if isinstance(value, Field) else value


def _row_of(element: Any, *, create: bool = True) -> dict[str, Any] | None:
    """The extracted-columns dict on an element's core (a plain dict element is
    its own row). ``create`` seeds an empty row on first access."""
    if isinstance(element, dict):
        return element
    core = getattr(element, "_core", None)
    if core is None:
        return None
    if core._row is None and create:
        core._row = {}
    return core._row


class Collection:
    """A set of results (elements or rows). Iterable/indexable; the row-shaping
    ops evaluate sub-expressions per element."""

    __slots__ = ("_items", "_client", "name", "root")

    def __init__(self, items: list[Any] | None = None, *, client: Any = None,
                 root: str = "") -> None:
        self._items = items or []
        self._client = client
        self.root = root
        self.name = f"col:{root}" if root else "col:"

    # -- container ------------------------------------------------------------
    def __iter__(self) -> Iterator[Any]:
        return iter(self._items)

    def __len__(self) -> int:
        return len(self._items)

    def __getitem__(self, i: int) -> Any:
        return self._items[i]

    def __repr__(self) -> str:
        return f"Collection({len(self._items)} items)"

    def __getattr__(self, name: str) -> Any:
        """An element op on the collection fans out over its elements: a list
        of results (a Collection when the results are surfaces)."""
        if name.startswith("_"):
            raise AttributeError(name)

        def fan(*args: Any, **kwargs: Any) -> Any:
            results = [getattr(el, name)(*args, **kwargs) for el in self._items]
            if results and all(hasattr(r, "_core") for r in results):
                return Collection(results, client=self._client, root=self.root)
            return results
        return fan

    # -- row shaping ----------------------------------------------------------
    def extract(self, **exprs: Any) -> "Collection":
        """Annotate each element with extracted columns (its ``_row``), then
        return a collection over the same elements. Columns are evaluated in
        order against the element, so a later column can reference an earlier
        one via ``field``; a chained extract accumulates into the same row.
        Field results are stored unwrapped (missing -> None)."""
        from .errors import RETURN, default_policy
        from .executor import evaluate
        with default_policy(RETURN):                  # a missing field is None, not an abort
            for el in self._items:
                row = _row_of(el)
                if row is None:
                    continue
                for key, expr in exprs.items():
                    row[key] = _raw(evaluate(expr, el, client=self._client))
        return self._derive(self._items)

    def filter(self, *predicates: Any) -> "Collection":
        """Keep the elements for which every predicate is truthy."""
        from .errors import RETURN, default_policy
        from .executor import evaluate, truthy
        with default_policy(RETURN):
            kept = [el for el in self._items
                    if all(truthy(evaluate(p, el, client=self._client))
                           for p in predicates)]
        return self._derive(kept)

    def documents(self, column: str) -> "Collection":
        """Flatten a column whose values are Collections/lists of documents
        into one Collection of those documents."""
        out: list[Any] = []
        for el in self._items:
            value = (_row_of(el) or {}).get(column)
            out.extend(list(value) if value is not None else [])
        return self._derive(out)

    def limit(self, n: int) -> "Collection":
        """Keep at most the first ``n`` elements."""
        return self._derive(self._items[:n])

    def project(self) -> list[Any]:
        """Materialise as a plain list: each element's extracted row if it has
        one, else the element itself."""
        out: list[Any] = []
        for el in self._items:
            row = _row_of(el, create=False)
            out.append(row if row is not None else el)
        return out

    def _derive(self, items: list[Any]) -> "Collection":
        out = Collection(items, client=self._client, root=self.root)
        out.name = self.name
        return out


__all__ = ["Collection", "Field"]
