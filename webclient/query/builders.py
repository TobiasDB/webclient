"""Query builders: the free plan-construction helpers behind ``wq``.

These build ``Expr`` plans (a ``reference(url)`` root, the ``when(...).then(...).otherwise(...)``
conditional, the ``filter`` / ``is_ok`` / ``is_empty`` / ``field`` recorder ops), so they
live with the query engine (``expr`` / ``plan`` / ``executor``) rather than in the surface
module. They are exposed through the ``wq`` namespace (``wq.when`` / ``wq.filter`` / ...),
which ``webclient.interface`` wires up; their static return types are the lazy surfaces,
referenced only under ``TYPE_CHECKING`` so this stays free of an import cycle.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, TypeVar, cast

from .expr import Expr, to_arg
from .plan import Plan, Step

if TYPE_CHECKING:
    from ..interface import LazyCollection, LazyField, Reference

T = TypeVar("T")

_MISSING: Any = object()

#: a Document-rooted lazy root, used by ``field`` (kept local so builders don't depend on
#: the ``wq.doc`` root defined in the surface module).
_doc: Any = Expr(Plan(root="Document"))


def reference(url: str, **kwargs: Any) -> "Reference":
    """A lazy reference root starting from ``url``: an ``Expr`` recording a plan
    rooted at that request spec (statically a ``Reference``)."""
    from ..core.reference import from_url

    spec = from_url(url, **kwargs).model_dump()
    return cast("Reference", Expr(Plan(root="Reference", source=spec)))


def field(name: str) -> Any:
    """A value already extracted in the surrounding row/context."""
    return _doc.field(name)


def _fn(name: str, expr: Any) -> Expr:
    """Record a named ``fn`` step onto ``expr`` (or a fresh plan) -- the shared builder behind
    the free-function op forms (``is_ok`` / ``is_empty`` / …)."""
    base = expr if isinstance(expr, Expr) else Expr(Plan())
    return base._extend(Step(kind="fn", name=name))


def is_empty(expr: Any) -> "LazyField[bool]":
    """Free-function form of ``x.is_empty()`` (records an ``fn`` step)."""
    return cast("LazyField[bool]", _fn("is_empty", expr))


def is_ok(expr: Any) -> "LazyField[bool]":
    """Free-function form of ``x.is_ok()`` (records an ``fn`` step)."""
    return cast("LazyField[bool]", _fn("is_ok", expr))


class _When:
    """Polars-style branching builder: ``when(cond).then(a).otherwise(b)`` -- a
    free construct recording a single ``when`` step whose parts are
    sub-expressions evaluated against the surrounding context."""

    __slots__ = ("_cond", "_then")

    def __init__(self, cond: Any) -> None:
        self._cond = cond
        self._then: Any = _MISSING

    def then(self, value: Any) -> "_When":
        """Set the value taken when the condition is truthy; returns self so ``.otherwise`` chains."""
        self._then = value
        return self

    def otherwise(self, value: Any) -> Any:
        """Set the else-value and finish the branch -- returns the ``Expr`` recording the whole
        ``when/then/otherwise``. Raises if ``.then(...)`` was never called."""
        if self._then is _MISSING:
            raise TypeError("when(...).then(...) before .otherwise(...)")
        step = Step(
            kind="when", args=[to_arg(self._cond), to_arg(self._then), to_arg(value)]
        )
        return Expr(Plan(steps=[step]))


def when(cond: Any) -> _When:
    """Start a Polars-style conditional: ``when(cond).then(a).otherwise(b)``."""
    return _When(cond)


def filter(collection: "LazyCollection[T]", *predicates: Any) -> "LazyCollection[T]":
    """Free-function form of the collection filter: ``filter(coll, pred)`` =
    ``coll.filter(pred)`` -- keeps the elements every predicate is truthy for."""
    return collection.filter(*predicates)


__all__ = [
    "reference", "field", "is_empty", "is_ok", "when", "filter", "_When",
]
