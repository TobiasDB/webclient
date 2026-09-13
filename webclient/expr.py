"""Expr: the lazy recorder -- Core-agnostic, records into a ``Plan``.

Every attribute access, call or operator on an ``Expr`` returns a new ``Expr``
extending its plan; nothing runs until the executor walks it (via
``Expr.collect`` / ``WebClient.execute``). The recorder knows nothing about the
cores or surfaces -- the one safety boundary is that a ``_``-prefixed name is
never recordable. Static types come from the generated surface stubs the roots
are cast to (see ``scripts.gen_stubs``); at runtime every value in a chain is an
``Expr``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, TypeVar, cast

from .plan import Arg, Plan, Step

T = TypeVar("T")


class _Missing:
    pass


_MISSING: Any = _Missing()


class Expr:
    """A recorded chain rooted at a ``Plan`` (optionally bound to a client)."""

    __slots__ = ("_plan", "_client")

    def __init__(self, plan: Plan, client: Any = None) -> None:
        object.__setattr__(self, "_plan", plan)
        object.__setattr__(self, "_client", client)

    # -- recording -----------------------------------------------------------
    def _extend(self, step: Step) -> "Expr":
        return Expr(self._plan.extend(step), self._client)

    def __getattr__(self, name: str) -> "Expr":
        if name.startswith("_"):  # the one safety boundary
            raise AttributeError(name)
        return self._extend(Step(kind="get", name=name))

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        eager = kwargs.pop("_collect", False)  # per-call eager escape hatch
        nxt = self._extend(
            Step(
                kind="call",
                args=[to_arg(a) for a in args],
                kwargs={k: to_arg(v) for k, v in kwargs.items()},
            )
        )
        return nxt.collect() if eager else nxt

    def _op(self, name: str, other: Any = _MISSING) -> "Expr":
        args = [] if other is _MISSING else [to_arg(other)]
        return self._extend(Step(kind="op", name=name, args=args))

    def __eq__(self, o: Any) -> "Expr":
        return self._op("eq", o)  # type: ignore[override]

    def __ne__(self, o: Any) -> "Expr":
        return self._op("ne", o)  # type: ignore[override]

    def __lt__(self, o: Any) -> "Expr":
        return self._op("lt", o)

    def __le__(self, o: Any) -> "Expr":
        return self._op("le", o)

    def __gt__(self, o: Any) -> "Expr":
        return self._op("gt", o)

    def __ge__(self, o: Any) -> "Expr":
        return self._op("ge", o)

    def __and__(self, o: Any) -> "Expr":
        return self._op("and", o)

    def __or__(self, o: Any) -> "Expr":
        return self._op("or", o)

    def __invert__(self) -> "Expr":
        return self._op("not")

    __hash__ = None  # type: ignore[assignment]

    def _coerce(self, what: str) -> Any:
        raise TypeError(
            f"a lazy expression has no {what}: it records, it does not run. "
            "Use it inside extract(...) / filter(...) or with `& | ~` -- not "
            "and/or/not/bool/len/iter."
        )

    def __bool__(self) -> bool:
        return self._coerce("truth value")

    def __len__(self) -> int:
        return self._coerce("length")

    def __iter__(self) -> Any:
        return self._coerce("iterator")

    # -- evaluation ----------------------------------------------------------
    def collect(self, context: Any = None) -> Any:
        """Evaluate this plan and return the result -- the single lazy trigger.
        A plan bound to a remote backend round-trips over HTTP; otherwise it
        runs on the local engine. ``collect`` is reserved (non-recordable)."""
        client = self._client
        if client is not None and hasattr(client, "remote_execute"):
            return client.remote_execute(self, context)
        from .executor import evaluate

        if client is None:
            from .core.client_core import WebClientCore

            client = WebClientCore()  # process-local default (MVP)
        from .collection import Field

        result = evaluate(self, context, client=client)
        if isinstance(result, Field):
            return result
        if isinstance(result, (str, int, float, bool)) or result is None:
            return Field(result)  # a scalar leaf -> a Field
        return result

    @property
    def is_lazy(self) -> bool:
        return True

    def __repr__(self) -> str:
        return f"lazy {self._plan.describe()}"


def to_arg(value: Any) -> Arg:
    if isinstance(value, Expr):
        return Arg(plan=value._plan)
    return Arg(value=value)


def lazy(cls: type[T], *, plan: Plan | None = None, client: Any = None) -> T:
    """A recording root for ``cls`` -- statically ``cls``, at runtime an
    ``Expr``."""
    return cast(T, Expr(plan or Plan(root=cls.__name__), client))


def from_plan(plan: Plan | dict[str, Any], client: Any = None) -> Expr:
    """Rebuild an ``Expr`` from its wire form (a Plan or its dict), validating
    its names first -- the wire safety boundary for the service/remote."""
    if isinstance(plan, dict):
        plan = Plan.model_validate(plan)
    plan.validate_names()
    return Expr(plan, client)


# --------------------------------------------------------------------------- #
# Roots and free functions
# --------------------------------------------------------------------------- #


def reference(url: str, **kwargs: Any) -> "Reference":
    """A lazy reference root starting from ``url``: an ``Expr`` recording a plan
    rooted at that request spec (statically a ``Reference``)."""
    from .core.reference_core import from_url

    spec = from_url(url, **kwargs).model_dump()
    return Expr(Plan(root="Reference", source=spec))


def field(name: str) -> Any:
    """A value already extracted in the surrounding row/context."""
    return doc.field(name)


def _fn(name: str, expr: Any) -> Expr:
    base = expr if isinstance(expr, Expr) else Expr(Plan())
    return base._extend(Step(kind="fn", name=name))


def is_empty(expr: Any) -> Any:
    """Free-function form of ``x.is_empty()`` (records an ``fn`` step)."""
    return _fn("is_empty", expr)


def is_ok(expr: Any) -> Any:
    """Free-function form of ``x.is_ok()`` (records an ``fn`` step)."""
    return _fn("is_ok", expr)


class _When:
    """Polars-style branching builder: ``when(cond).then(a).otherwise(b)`` -- a
    free construct recording a single ``when`` step whose parts are
    sub-expressions evaluated against the surrounding context."""

    __slots__ = ("_cond", "_then")

    def __init__(self, cond: Any) -> None:
        self._cond = cond
        self._then: Any = _MISSING

    def then(self, value: Any) -> "_When":
        self._then = value
        return self

    def otherwise(self, value: Any) -> Any:
        if self._then is _MISSING:
            raise TypeError("when(...).then(...) before .otherwise(...)")
        step = Step(
            kind="when", args=[to_arg(self._cond), to_arg(self._then), to_arg(value)]
        )
        return Expr(Plan(steps=[step]))


def when(cond: Any) -> _When:
    """Start a Polars-style conditional: ``when(cond).then(a).otherwise(b)``."""
    return _When(cond)


def filter(collection: Any, *predicates: Any) -> Any:
    """Free-function form of the collection filter: ``filter(coll, pred)`` =
    ``coll.filter(pred)``."""
    return collection.filter(*predicates)


#: the lazy roots -- an ``Expr`` rooted at each surface (statically the surface
#: it authors plans for; at runtime an ``Expr``).
if TYPE_CHECKING:
    from .collection import Collection
    from .surfaces import Document, Reference

    doc: "Document"
    ref: "Reference"
    many: "Collection[Document]"
else:
    doc = Expr(Plan(root="Document"))
    ref = Expr(Plan(root="Reference"))
    many = Expr(Plan(root="Collection"))


__all__ = [
    "Expr",
    "lazy",
    "from_plan",
    "to_arg",
    "reference",
    "field",
    "when",
    "filter",
    "is_empty",
    "is_ok",
    "doc",
    "ref",
    "many",
]
