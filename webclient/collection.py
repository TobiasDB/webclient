"""Collection and Field: the two surface types that carry a little behaviour.

Every other surface is a method-free wrapper over a core (``webclient.surface``),
but a ``Collection`` (a fanned-out set of results) and a ``Field`` (a scalar
leaf) are the sanctioned exceptions -- they hold minimal built-in helpers.
``Collection`` runs the row-shaping ops (``extract`` / ``filter`` / ``project``)
by evaluating sub-expressions against each element; ``Field`` is the value leaf
(``get`` + comparisons + truthiness).
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
    def ok(self) -> bool:
        return self._ok

    def __bool__(self) -> bool:
        return bool(self._value) if self._ok else False

    def __eq__(self, o: Any) -> bool:   # type: ignore[override]
        return self.get() == (o.get() if isinstance(o, Field) else o)

    def __repr__(self) -> str:
        return f"Field({self._value!r})" if self._ok else "Field(<empty>)"


class Collection:
    """A set of results (elements or rows). Iterable/indexable; the row-shaping
    ops evaluate sub-expressions per element."""

    __slots__ = ("_items", "_client", "name")

    def __init__(self, items: list[Any] | None = None, *, client: Any = None,
                 name: str | None = None) -> None:
        self._items = items or []
        self._client = client
        self.name = name or ""

    # -- container ------------------------------------------------------------
    def __iter__(self) -> Iterator[Any]:
        return iter(self._items)

    def __len__(self) -> int:
        return len(self._items)

    def __getitem__(self, i: int) -> Any:
        return self._items[i]

    def __repr__(self) -> str:
        return f"Collection({len(self._items)} items)"

    # -- row shaping ----------------------------------------------------------
    def extract(self, **exprs: Any) -> "Collection":
        """One row per element: each keyword's expression is evaluated against
        that element (a prior row's fields are merged in, so chained extracts
        can reference earlier columns)."""
        from .executor import evaluate
        rows: list[dict[str, Any]] = []
        for el in self._items:
            base = dict(el) if isinstance(el, dict) else {}
            row = {**base, **{k: evaluate(e, el, client=self._client)
                              for k, e in exprs.items()}}
            rows.append(row)
        return Collection(rows, client=self._client, name=self.name)

    def filter(self, *predicates: Any) -> "Collection":
        """Keep the elements/rows for which every predicate is truthy."""
        from .executor import evaluate, truthy
        kept = [el for el in self._items
                if all(truthy(evaluate(p, el, client=self._client))
                       for p in predicates)]
        return Collection(kept, client=self._client, name=self.name)

    def limit(self, n: int) -> "Collection":
        """Keep at most the first ``n`` elements."""
        return Collection(self._items[:n], client=self._client, name=self.name)

    def project(self) -> list[Any]:
        """Materialise the rows/elements as a plain list."""
        return list(self._items)


__all__ = ["Collection", "Field"]
