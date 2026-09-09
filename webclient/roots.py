"""The lazy roots.

`doc`, `el`, `ref` and `err` are ordinary instances of the ordinary classes
with recording switched on. There is no separate lazy class hierarchy, no
generated stub and no codegen: the signatures a type checker sees are the
signatures the eager path uses, because they are the same methods.
"""
from __future__ import annotations

from typing import Any, TypeVar

from .document import Document, Element
from .plan import CallStep, Plan, Source
from .records import Err, Record, RecordSet
from .reference import Reference
from .values import (DROP_ROW, NULL, RAISE_ERROR, ExprState, Selection, Value,
                     _to_arg)

T = TypeVar("T")


def _lazy(cls: type[T], root: str | None = None) -> T:
    obj = cls.__new__(cls)                          # type: ignore[misc]
    object.__setattr__(
        obj, "_expr",
        ExprState(plan=Plan(source=Source(kind="context",
                                          root=root or cls.__name__))))
    obj._init_lazy()                                # type: ignore[attr-defined]
    return obj


#: the document being projected
doc: Document = _lazy(Document)
#: the current element inside `map`
el: Element = _lazy(Element)
#: a reference — call it to root a plan at a URL
ref: Reference = _lazy(Reference)
#: the failure, inside `otherwise(...)`
err: Err = _lazy(Err)


def field(name: str, *, optional: bool = False) -> Any:
    """A value already extracted in this context: the record being built in a
    plan, or `doc.fields` on a Document.

    Typed `Any` deliberately — a column's type is a run-time fact. Claiming
    otherwise would block `field("link").resolve()`, which is the whole point
    of naming a column.
    """
    plan = Plan(source=Source(kind="context", root="Document"),
                steps=[CallStep(op="field", args=[_to_arg(name)],
                                kwargs={"optional": _to_arg(optional)})])
    value: Value[Any] = Value.__new__(Value)
    object.__setattr__(value, "_expr", ExprState(plan=plan))
    value._init_lazy()
    return value


def lit(value: Any) -> Value[Any]:
    """A literal, for the rare place a plain Python value is ambiguous."""
    from .plan import LiteralStep
    plan = Plan(steps=[LiteralStep(value=value)])
    wrapper: Value[Any] = Value.__new__(Value)
    object.__setattr__(wrapper, "_expr", ExprState(plan=plan))
    wrapper._init_lazy()
    return wrapper


__all__ = ["doc", "el", "ref", "err", "field", "lit",
           "RAISE_ERROR", "DROP_ROW", "NULL"]
