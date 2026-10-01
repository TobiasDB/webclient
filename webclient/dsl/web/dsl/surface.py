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

import ast
import operator
from collections.abc import Sequence
from typing import (
    TYPE_CHECKING,
    Literal,
    Protocol,
    TypeVar,
    cast,
    overload,
    runtime_checkable,
)

from pydantic import JsonValue

from .expr import Expr, to_arg
from .plan import Plan, Step
from .values import Collection, Field, Ref

if TYPE_CHECKING:
    from web.parse import Document, Element
    from web.resolve import Resolver

#: the element a collection yields (covariant: a collection only ever PRODUCES its elements, so a
#: ``LazyCollection[str]`` is usable where a ``LazyCollection[object]`` -- e.g. an extract column --
#: is expected).
T = TypeVar("T", covariant=True)


@runtime_checkable
class LazyField(Protocol):
    """A recorded scalar-leaf chain (a value read). Collects to its value."""

    def number(self, default: object = None) -> "LazyField": ...
    def date(
        self,
        format: str | None = None,
        *,
        dayfirst: bool = False,
        default: object = None,
    ) -> "LazyField": ...
    def datetime(
        self,
        format: str | None = None,
        *,
        dayfirst: bool = False,
        default: object = None,
    ) -> "LazyField": ...
    def split(
        self,
        sep: str | None = None,
        maxsplit: int = -1,
        *,
        regex: bool = False,
        strip: bool = True,
        keep_empty: bool = False,
    ) -> "LazyCollection[str]": ...
    def map(self, mapping: "dict[str, JsonValue]", default: object = None) -> "LazyField": ...
    def link(self, base: str | None = None) -> "LazyField": ...
    def regex(
        self, pattern: str, *, group: "int | str" = 0, flags: int = 0, default: object = None
    ) -> "LazyField": ...
    def is_ok(self) -> "LazyField": ...
    def is_empty(self) -> "LazyField": ...
    def collect(self, root: object = None, *, resolver: "Resolver | None" = None) -> JsonValue: ...
    async def acollect(
        self, root: object = None, *, resolver: "Resolver | None" = None
    ) -> JsonValue: ...
    def to_blob(self) -> str: ...
    def describe(self) -> str: ...
    def to_source(self) -> str: ...
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

    def select(self, selector: str, *, optional: bool = False) -> "LazyCollection[Element]": ...
    def select_all(self, selector: str) -> "LazyCollection[Element]": ...
    @overload
    def attr(self, name: "Literal['href', 'src']") -> "LazyCollection[Ref]": ...  # type: ignore[overload-overlap]
    @overload
    def attr(self, name: str) -> "LazyCollection[str]": ...
    def text(self) -> "LazyCollection[str]": ...
    def links(self) -> "LazyCollection[Ref]": ...
    def resolve(
        self,
        *,
        profile: str | None = None,
        paginate: str | None = None,
        max_pages: int = 20,
        rate_limit: float | None = None,
        retry: int | None = None,
        rotate: bool | None = None,
        raise_on_error: bool = True,
        optional: bool = False,
    ) -> "LazyCollection[Document]": ...  # follow refs
    def number(self, default: object = None) -> "LazyCollection[JsonValue]": ...
    def date(
        self,
        format: str | None = None,
        *,
        dayfirst: bool = False,
        default: object = None,
    ) -> "LazyCollection[JsonValue]": ...
    def extract(
        self,
        **columns: "LazyField | LazyCollection[object] | LazyDocument | LazyReference | JsonValue",
    ) -> "LazyCollection[dict[str, JsonValue]]": ...
    def filter(
        self, *predicates: "LazyField | LazyCollection[object] | JsonValue"
    ) -> "LazyCollection[T]": ...
    def project(
        self, *, flatten: bool = False, sep: str = ".", distinct: bool = False
    ) -> "LazyCollection[dict[str, JsonValue]]": ...
    def merge(self) -> "LazyField": ...
    def documents(self, column: str) -> "LazyCollection[Document]": ...
    def limit(self, n: int) -> "LazyCollection[T]": ...
    def skip(self, n: int) -> "LazyCollection[T]": ...
    def first(self, default: object = None) -> "LazyField": ...
    def last(self, default: object = None) -> "LazyField": ...
    def distinct(self) -> "LazyCollection[T]": ...
    def identity(self, *parts: str) -> "LazyCollection[dict[str, JsonValue]]": ...  # per row
    def reference(self, name: str) -> "LazyReference": ...
    def field(self, name: str) -> "LazyField": ...
    def collect(
        self, root: object = None, *, resolver: "Resolver | None" = None
    ) -> "Sequence[T]": ...
    async def acollect(
        self, root: object = None, *, resolver: "Resolver | None" = None
    ) -> "Sequence[T]": ...
    def to_blob(self) -> str: ...
    def describe(self) -> str: ...
    def to_source(self) -> str: ...


class LazyDocument(Protocol):
    """A recorded single-document chain (a resolved page, or a nested ``select``). Collects to a
    :class:`~web.parse.Document`; reads narrow to a field / reference / collection."""

    def doc(self) -> "LazyDocument": ...
    def select(self, selector: str, *, optional: bool = False) -> "LazyDocument": ...
    def select_all(self, selector: str) -> "LazyCollection[Element]": ...
    def next(self, selector: str = "", *, optional: bool = False) -> "LazyDocument": ...
    def prev(self, selector: str = "", *, optional: bool = False) -> "LazyDocument": ...
    @overload
    def attr(self, name: "Literal['href', 'src']") -> "LazyReference": ...  # type: ignore[overload-overlap]
    @overload
    def attr(self, name: str) -> "LazyField": ...
    def text(self) -> "LazyField": ...
    def links(self) -> "LazyCollection[Ref]": ...
    def reference(self, name: str) -> "LazyReference": ...
    def field(self, name: str) -> "LazyField": ...

    # the parse-Document read methods, exposed lazily with their real signatures (they run via the
    # executor's method dispatch); each collects to its value.
    def markdown(self, *, main_content_only: bool = False) -> "LazyField": ...
    def readable(self, *, main_content_only: bool = True) -> "LazyField": ...
    def tables(self, selector: "str | None" = None, *, transpose: bool = False) -> "LazyField": ...
    def skeleton(
        self,
        *,
        max_lines: int = 400,
        text_chars: int = 40,
        max_depth: int = 30,
        mark_records: bool = True,
        mark_interactive: bool = True,
        drop_chrome: bool = False,
    ) -> "LazyField": ...
    def regex(self, pattern: str, *, group: "int | str" = 0, flags: int = 0) -> "LazyField": ...
    def at(self, path: str) -> "LazyField": ...
    def metadata(self) -> "LazyField": ...
    def records(self, *, min_items: int = 3, top_k: int = 3) -> "LazyField": ...
    def identity(self, *parts: str) -> "LazyField": ...  # a stable identity of this document
    def collect(self, root: object = None, *, resolver: "Resolver | None" = None) -> "Document": ...
    async def acollect(
        self, root: object = None, *, resolver: "Resolver | None" = None
    ) -> "Document": ...
    def to_blob(self) -> str: ...
    def describe(self) -> str: ...
    def to_source(self) -> str: ...


class LazyReference(Protocol):
    """A recorded reference chain (a request spec / a link from ``attr('href')``). ``resolve()``
    fetches it into a document; collected without resolving, it reads as a :class:`~web.dsl.Ref`.
    """

    def resolve(
        self,
        *,
        profile: str | None = None,
        paginate: str | None = None,
        max_pages: int = 20,
        rate_limit: float | None = None,
        retry: int | None = None,
        rotate: bool | None = None,
        raise_on_error: bool = True,
        optional: bool = False,
    ) -> "LazyDocument":
        """Fetch this reference into a document. ``profile`` names a default resolve policy
        (``"basic"`` / ``"basic_browser"`` / ``"full_browser"``); ``paginate`` / ``max_pages`` /
        ``rate_limit`` / ``retry`` / ``rotate`` / ``raise_on_error`` are the per-step policy;
        ``optional`` tolerates a miss / transport failure (-> ``None``). All args are JSON-safe so
        the plan stays serialisable."""
        ...

    def collect(self, root: object = None, *, resolver: "Resolver | None" = None) -> "Ref": ...
    async def acollect(
        self, root: object = None, *, resolver: "Resolver | None" = None
    ) -> "Ref": ...
    def to_blob(self) -> str: ...
    def describe(self) -> str: ...
    def to_source(self) -> str: ...


class LazyThen(LazyField, Protocol):
    """A ``when(cond).then(a)`` conditional -- already usable as a value (``otherwise`` defaults to
    ``None``), and ``.otherwise(b)`` refines it with the else-branch."""

    def otherwise(self, value: object) -> "LazyField": ...


class _WhenExpr(Expr):
    """The recorded ``when`` branch: an :class:`Expr` (so it works as a column / predicate straight
    after ``.then(...)``, with a ``None`` else), plus ``.otherwise(...)`` to set the else-branch.
    """

    def __init__(self, cond: object, then: object, otherwise: object = None) -> None:
        step = Step(kind="when", args=[to_arg(cond), to_arg(then), to_arg(otherwise)])
        super().__init__(Plan(steps=[step]))
        object.__setattr__(self, "_cond", cond)
        object.__setattr__(self, "_then", then)

    def otherwise(self, value: object) -> LazyField:
        """Set the else-branch (taken when the condition is falsy) and return the recorded branch."""
        return cast(LazyField, _WhenExpr(self._cond, self._then, value))


class _When:
    """The ``when(cond)`` conditional builder: ``.then(a)`` is already a usable value (else ``None``),
    ``.then(a).otherwise(b)`` sets the else-branch."""

    __slots__ = ("_cond",)

    def __init__(self, cond: object) -> None:
        self._cond = cond

    def then(self, value: object) -> LazyThen:
        """The value taken when the condition is truthy (else ``None`` until ``.otherwise`` is set)."""
        return cast(LazyThen, _WhenExpr(self._cond, value))


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

    def filter(
        self,
        collection: "LazyCollection[T]",
        *predicates: "LazyField | LazyCollection[object] | JsonValue",
    ) -> "LazyCollection[T]":
        """Free-function form of the collection filter: ``filter(coll, pred)`` == ``coll.filter(pred)``."""
        return collection.filter(*predicates)

    def is_ok(self, expr: LazyField) -> LazyField:
        """Free-function form of ``x.is_ok()``."""
        return expr.is_ok()

    def is_empty(self, expr: LazyField) -> LazyField:
        """Free-function form of ``x.is_empty()``."""
        return expr.is_empty()


wq = _Wq()


#: literal constants a source may contain; comparison ops a ``filter`` predicate may use.
_CONST = (str, int, float, bool, type(None))
_CMP: "dict[type[ast.cmpop], object]" = {
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
}


class SourceError(ValueError):
    """A string was not a rebuildable ``wq`` functional expression (unparseable / disallowed)."""


def _eval_src(node: "ast.AST", *, table: bool = False) -> object:
    """Interpret ONE ``wq`` source AST node against the real recorder. Only ``wq`` is a name; a
    ``_``-prefixed attribute, ``*``/``**`` args, or any other construct is refused -- so an untrusted
    source cannot execute arbitrary code (``wq.reference.__globals__[...]`` is rejected at ``_``).
    ``table`` allows a dict literal (the argument of ``.map({...})``).
    """
    if isinstance(node, ast.Expression):
        return _eval_src(node.body)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, _CONST):
            return node.value
        raise SourceError(f"disallowed constant: {node.value!r}")
    if isinstance(node, ast.Name):
        if node.id == "wq":
            return wq
        raise SourceError(f"only 'wq' is available in a query, not {node.id!r}")
    if isinstance(node, ast.Attribute):
        if node.attr.startswith("_"):
            raise SourceError(f"attribute {node.attr!r} is not allowed in a query")
        return getattr(_eval_src(node.value), node.attr)
    if isinstance(node, ast.Call):
        if any(isinstance(a, ast.Starred) for a in node.args):
            raise SourceError("*args are not allowed in a query")
        func = _eval_src(node.func)
        is_map = isinstance(node.func, ast.Attribute) and node.func.attr == "map"
        args = [_eval_src(a, table=is_map) for a in node.args]
        kwargs: "dict[str, object]" = {}
        for kw in node.keywords:
            if kw.arg is None:
                raise SourceError("**kwargs are not allowed in a query")
            kwargs[kw.arg] = _eval_src(kw.value, table=is_map and kw.arg == "mapping")
        return func(*args, **kwargs)  # type: ignore[operator]
    if isinstance(node, ast.Dict):  # a literal table -- ONLY as the argument of `.map({...})`
        if not table:
            raise SourceError(
                "a dict literal is only allowed as the table of .map({...}); an extract column "
                "is a wq.doc chain, a nested branch a nested extract(...)"
            )
        if any(k is None for k in node.keys):
            raise SourceError("** unpacking is not allowed in a query")
        return {_eval_src(k): _eval_src(v) for k, v in zip(node.keys, node.values) if k is not None}
    if isinstance(node, (ast.List, ast.Tuple)):  # a literal list of values
        return [_eval_src(e) for e in node.elts]
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Invert):  # ~cond
        return ~_eval_src(node.operand)  # type: ignore[operator]
    if isinstance(node, ast.BinOp) and isinstance(
        node.op, (ast.BitAnd, ast.BitOr)
    ):  # a & b / a | b
        left, right = _eval_src(node.left), _eval_src(node.right)
        return left & right if isinstance(node.op, ast.BitAnd) else left | right  # type: ignore[operator]
    if isinstance(node, ast.Compare) and len(node.ops) == 1:  # a == b, a < b, ...
        fn = _CMP.get(type(node.ops[0]))
        if fn is None:
            raise SourceError("that comparison is not allowed in a query")
        return fn(_eval_src(node.left), _eval_src(node.comparators[0]))  # type: ignore[operator]
    raise SourceError(f"disallowed expression in a query: {type(node).__name__}")


def from_source(src: str) -> Expr:
    """Rebuild an :class:`Expr` from a ``wq`` FUNCTIONAL source string -- the inverse of
    :meth:`Expr.to_source` (e.g. ``wq.reference('u').resolve(profile='basic').select_all('.row')``).
    It drives the REAL recorder, never ``eval``: only ``wq`` is in scope and a private attribute,
    ``*``/``**`` args or any other construct is refused, so an untrusted source is safe to rebuild.
    Raises :class:`SourceError`."""
    text = src.strip()
    if not text:
        raise SourceError("empty source")
    if "\n" in text:  # a chain written over several lines (leading-dot continuations) -- join them
        text = f"({text}\n)"  # the paren on its own line: a trailing `# comment` must not eat it
    try:
        tree = ast.parse(text, mode="eval")
    except SyntaxError as exc:
        raise SourceError(f"source did not parse: {exc}") from exc
    result = _eval_src(tree)
    if not isinstance(result, Expr):
        raise SourceError(f"source is a {type(result).__name__}, not a wq chain")
    bad = unknown_verbs(result)
    if bad:
        raise UnknownVerb(bad, used=verbs_of(result))
    return result


class UnknownVerb(SourceError):
    """The source used verb(s) the DSL does not have (``.join(...)``, ``.first()``, ...) -- refused
    at parse time so a model-written chain fails LOUDLY, naming the gap, instead of yielding nulls
    at run time. ``verbs`` lists the unknown ones; ``used`` every verb the source called."""

    def __init__(self, verbs: "Sequence[str]", *, used: "Sequence[str]" = ()) -> None:
        self.verbs = list(verbs)
        self.used = list(used)
        super().__init__(
            f"unknown DSL verb(s): {', '.join(self.verbs)} -- not in the wq surface (a chain may "
            "only use the documented verbs)"
        )


def _surface_names(*classes: type) -> "frozenset[str]":
    return frozenset(n for c in classes for n in vars(c) if not n.startswith("_"))


#: every VERB the ``wq`` surface has -- the public names of the lazy tiers + the ``wq`` roots /
#: builders. The single answer to "is this a DSL verb?" (:func:`unknown_verbs`, the runtime).
KNOWN_VERBS: "frozenset[str]" = _surface_names(
    LazyField, LazyCollection, LazyDocument, LazyReference, LazyThen, _Wq
) | frozenset({"doc", "ref", "then", "otherwise"})


def verbs_of(expr: Expr) -> "list[str]":
    """Every verb the chain calls, in order, nested sub-expressions included (each ``get`` step) --
    for tallying what an author actually reaches for."""
    out: list[str] = []

    def walk(plan: Plan) -> None:
        for step in plan.steps:
            if step.kind == "get":
                out.append(step.name)
            for arg in (*step.args, *step.kwargs.values()):
                if arg.plan is not None:
                    walk(arg.plan)

    walk(expr._plan)
    return out


def unknown_verbs(expr: Expr) -> "list[str]":
    """The verbs in the chain that are NOT in :data:`KNOWN_VERBS` (deduplicated, in order)."""
    seen: list[str] = []
    for v in verbs_of(expr):
        if v not in KNOWN_VERBS and v not in seen:
            seen.append(v)
    return seen


__all__ = [
    "wq",
    "from_source",
    "SourceError",
    "LazyReference",
    "LazyDocument",
    "LazyCollection",
    "LazyField",
    "LazyThen",
    "Collection",
    "Field",
    "Ref",
]
