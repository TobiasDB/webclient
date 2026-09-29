"""The typed lazy surfaces + the ``wq`` builder namespace.

At runtime every node in a ``wq`` chain is an :class:`~web.dsl.expr.Expr` recording a plan; these
Protocols are a pure TYPING overlay the roots are ``cast`` to, so a chain is statically typed and --
most importantly -- each terminal returns a PRECISE type, never ``object`` / ``Any``. The one
collection surface is generic in its element type, :class:`LazyCollection`\\[T]: ``select_all`` ->
``LazyCollection[Element]``, ``.attr('href')`` -> ``LazyCollection[Ref]``, ``.resolve()`` ->
``LazyCollection[Document]``, ``.attr('text')`` -> ``LazyCollection[str]``, ``.extract(...)`` ->
``LazyCollection[dict]``, and ``.collect()`` -> ``list[T]``. This mirrors the monolith's generated
lazy surfaces -- same DSL, hand-written compact for the packages layer.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Literal, Protocol, TypeVar, cast, overload, runtime_checkable

from pydantic import JsonValue

from .expr import Expr
from .plan import Plan, Step
from .values import Collection, Field, Ref

if TYPE_CHECKING:
    from web.parse import Document, Element
    from web.resolve import Resolver

#: the element a collection yields (covariant: a collection only ever PRODUCES its elements, so a
#: ``LazyCollection[str]`` is usable where a ``LazyCollection[object]`` -- e.g. an extract column --
#: is expected).
T = TypeVar("T", covariant=True)
#: an extract column / filter predicate: any recorded sub-expression, or a literal constant column.
Sub = "LazyField | LazyCollection[object] | LazyDocument | LazyReference | JsonValue"


@runtime_checkable
class LazyField(Protocol):
    """A recorded scalar-leaf chain (a value read). Collects to its value."""

    def number(self, default: object = None) -> "LazyField": ...
    def date(self, format: str | None = None, *, dayfirst: bool = False, default: object = None) -> "LazyField": ...
    def datetime(self, format: str | None = None, *, dayfirst: bool = False, default: object = None) -> "LazyField": ...
    def split(self, sep: str | None = None, maxsplit: int = -1, *, regex: bool = False,
              strip: bool = True, keep_empty: bool = False) -> "LazyCollection[str]": ...
    def map(self, mapping: "dict[str, JsonValue]", default: object = None) -> "LazyField": ...
    def link(self, base: str | None = None) -> "LazyField": ...
    def is_ok(self) -> "LazyField": ...
    def is_empty(self) -> "LazyField": ...
    def collect(self, root: object = None, *, resolver: "Resolver | None" = None) -> JsonValue: ...
    async def acollect(self, root: object = None, *, resolver: "Resolver | None" = None) -> JsonValue: ...
    def to_blob(self) -> str: ...
    def describe(self) -> str: ...
    def __eq__(self, o: object) -> "LazyField": ...  # type: ignore[override]
    def __ne__(self, o: object) -> "LazyField": ...  # type: ignore[override]
    def __lt__(self, o: object) -> "LazyField": ...
    def __le__(self, o: object) -> "LazyField": ...
    def __gt__(self, o: object) -> "LazyField": ...
    def __ge__(self, o: object) -> "LazyField": ...
    def __and__(self, o: object) -> "LazyField": ...
    def __or__(self, o: object) -> "LazyField": ...
    def __invert__(self) -> "LazyField": ...


class LazyCollection(Protocol[T]):
    """A recorded set-of-results chain, generic in its element type ``T``. Navigation narrows the
    element type (``select_all`` -> ``[Element]``, ``attr('href')`` -> ``[Ref]``, ``resolve`` ->
    ``[Document]``, ``attr``/``text`` -> ``[str]``); ``extract`` / ``project`` yield row dicts. The
    smart terminal ``collect`` returns ``list[T]`` (extracted rows project automatically -- no
    explicit ``.project()`` needed)."""

    def select(self, selector: str) -> "LazyCollection[Element]": ...
    def select_all(self, selector: str) -> "LazyCollection[Element]": ...
    @overload
    def attr(self, name: "Literal['href', 'src']") -> "LazyCollection[Ref]": ...  # type: ignore[overload-overlap]
    @overload
    def attr(self, name: str) -> "LazyCollection[str]": ...
    def text(self) -> "LazyCollection[str]": ...
    def links(self) -> "LazyCollection[Ref]": ...
    def resolve(self) -> "LazyCollection[Document]": ...  # follow a collection of references
    def number(self, default: object = None) -> "LazyCollection[JsonValue]": ...
    def date(self, format: str | None = None, *, dayfirst: bool = False, default: object = None) -> "LazyCollection[JsonValue]": ...
    def extract(self, **columns: "LazyField | LazyCollection[object] | LazyDocument | LazyReference | JsonValue") -> "LazyCollection[dict[str, JsonValue]]": ...
    def filter(self, *predicates: "LazyField | LazyCollection[object] | JsonValue") -> "LazyCollection[T]": ...
    def project(self, *, flatten: bool = False, sep: str = ".", distinct: bool = False) -> "LazyCollection[dict[str, JsonValue]]": ...
    def merge(self) -> "LazyField": ...
    def documents(self, column: str) -> "LazyCollection[Document]": ...
    def limit(self, n: int) -> "LazyCollection[T]": ...
    def distinct(self) -> "LazyCollection[T]": ...
    def reference(self, name: str) -> "LazyReference": ...
    def field(self, name: str) -> "LazyField": ...
    def collect(self, root: object = None, *, resolver: "Resolver | None" = None) -> "Sequence[T]": ...
    async def acollect(self, root: object = None, *, resolver: "Resolver | None" = None) -> "Sequence[T]": ...
    def to_blob(self) -> str: ...
    def describe(self) -> str: ...


class LazyDocument(Protocol):
    """A recorded single-document chain (a resolved page, or a nested ``select``). Collects to a
    :class:`~web.parse.Document`; reads narrow to a field / reference / collection."""

    def doc(self) -> "LazyDocument": ...
    def select(self, selector: str) -> "LazyDocument": ...
    def select_all(self, selector: str) -> "LazyCollection[Element]": ...
    @overload
    def attr(self, name: "Literal['href', 'src']") -> "LazyReference": ...  # type: ignore[overload-overlap]
    @overload
    def attr(self, name: str) -> "LazyField": ...
    def text(self) -> "LazyField": ...
    def links(self) -> "LazyCollection[Ref]": ...
    def reference(self, name: str) -> "LazyReference": ...
    def field(self, name: str) -> "LazyField": ...
    def collect(self, root: object = None, *, resolver: "Resolver | None" = None) -> "Document": ...
    async def acollect(self, root: object = None, *, resolver: "Resolver | None" = None) -> "Document": ...
    def to_blob(self) -> str: ...
    def describe(self) -> str: ...


class LazyReference(Protocol):
    """A recorded reference chain (a request spec / a link from ``attr('href')``). ``resolve()``
    fetches it into a document; collected without resolving, it reads as a :class:`~web.dsl.Ref`."""

    def resolve(self) -> "LazyDocument": ...
    def collect(self, root: object = None, *, resolver: "Resolver | None" = None) -> "Ref": ...
    async def acollect(self, root: object = None, *, resolver: "Resolver | None" = None) -> "Ref": ...
    def to_blob(self) -> str: ...
    def describe(self) -> str: ...


class _When:
    """The ``when(cond).then(a).otherwise(b)`` conditional builder -- records a single ``when`` step
    whose three parts are sub-expressions evaluated against the surrounding element."""

    __slots__ = ("_cond", "_then")

    def __init__(self, cond: object) -> None:
        self._cond = cond
        self._then: object = _MISSING

    def then(self, value: object) -> "_When":
        """The value taken when the condition is truthy; returns self so ``.otherwise`` chains."""
        self._then = value
        return self

    def otherwise(self, value: object) -> LazyField:
        """The else-value; finishes the branch and returns the recorded ``Expr`` (as a LazyField)."""
        if self._then is _MISSING:
            raise TypeError("when(...).then(...) before .otherwise(...)")
        from .expr import to_arg

        step = Step(kind="when", args=[to_arg(self._cond), to_arg(self._then), to_arg(value)])
        return cast(LazyField, Expr(Plan(steps=[step])))


_MISSING: object = object()
_DOC: LazyDocument = cast(LazyDocument, Expr(Plan(root="Document")))


class _Wq:
    """The ``wq`` namespace: lazy roots (``wq.doc`` / ``wq.ref``) and the free builders
    (``reference`` / ``when`` / ``field`` / ``filter`` / ``is_ok`` / ``is_empty``)."""

    doc: LazyDocument = _DOC
    ref: LazyReference = cast(LazyReference, Expr(Plan(root="Reference")))

    def reference(self, url: str) -> LazyReference:
        """A lazy reference root starting from ``url`` -- the clean entry (no hand-built Request)."""
        return cast(LazyReference, Expr(Plan(root="Reference", source=url)))

    def field(self, name: str) -> LazyField:
        """A value already extracted in the surrounding row."""
        return _DOC.field(name)

    def when(self, cond: object) -> _When:
        """Start a conditional: ``when(cond).then(a).otherwise(b)``."""
        return _When(cond)

    def filter(self, collection: "LazyCollection[T]",
               *predicates: "LazyField | LazyCollection[object] | JsonValue") -> "LazyCollection[T]":
        """Free-function form of the collection filter: ``filter(coll, pred)`` == ``coll.filter(pred)``."""
        return collection.filter(*predicates)

    def is_ok(self, expr: LazyField) -> LazyField:
        """Free-function form of ``x.is_ok()``."""
        return expr.is_ok()

    def is_empty(self, expr: LazyField) -> LazyField:
        """Free-function form of ``x.is_empty()``."""
        return expr.is_empty()


wq = _Wq()


__all__ = ["wq", "LazyReference", "LazyDocument", "LazyCollection", "LazyField", "Collection",
           "Field", "Ref"]
