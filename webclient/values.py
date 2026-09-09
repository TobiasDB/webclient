"""`Expr`, `Value`, `Selection`, `Record` — the types every op returns.

One class in two modes is only honest if ops return wrappers: `attr` gives a
`Value[str]`, not a `str`, so `.alias()` / `.otherwise()` / `.map()` are
visible to a type checker whether the chain is being evaluated or recorded.
Eagerly a wrapper already holds its value; lazily it holds the plan that
would produce it.

`.get()` unwraps on the eager path. That is the whole tax.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import (Any, Callable, Generic, Iterator, Mapping, Sequence,
                    TypeVar, overload)

from typing_extensions import Self

from .errors import PlanError, WebClientError
from .plan import (Arg, BinOpStep, CallStep, ExplodeStep, FilterStep,
                   LimitStep, MapStep, OtherwiseStep, Plan, Projection,
                   Sentinel, Source, ThenStep)

T = TypeVar("T")

#: Result classes an op may name in `returns=`, filled in by each module as
#: it defines them (avoids import cycles between values/document/reference).
LAZY_TYPES: dict[str, type] = {}


def register_lazy_type(name: str, cls: type) -> None:
    LAZY_TYPES[name] = cls


@dataclass
class ExprState:
    """The recording half of a wrapper. `plan is None` means eager."""

    plan: Plan | None = None
    name: str | None = None


@dataclass
class Failure:
    """What `err` exposes inside a recovery block."""

    message: str
    op: str = ""
    kind: str = ""
    error: BaseException | None = None

    def __getattr__(self, item: str) -> Any:      # err.<anything> in a plan
        raise AttributeError(item)


# --------------------------------------------------------------------------- #
# Sentinels
# --------------------------------------------------------------------------- #

class _Sentinel:
    __slots__ = ("policy",)

    def __init__(self, policy: Sentinel) -> None:
        self.policy = policy

    def __repr__(self) -> str:
        return f"<{self.policy.upper()}>"


RAISE_ERROR = _Sentinel("raise")
DROP_ROW = _Sentinel("drop")
NULL = _Sentinel("null")


# --------------------------------------------------------------------------- #
# Expr — the shared combinator surface
# --------------------------------------------------------------------------- #

class Expr:
    """Combinators shared by every result type.

    Subclasses provide eager state and `_eager_context()`; everything here
    branches once on `is_lazy` and is otherwise a single implementation.
    """

    _expr: ExprState | None = None

    # -- mode ---------------------------------------------------------------
    @property
    def is_lazy(self) -> bool:
        return self._expr is not None and self._expr.plan is not None

    @property
    def _column_name(self) -> str | None:
        return self._expr.name if self._expr is not None else None

    def _plan_or_new(self) -> Plan:
        if self._expr is not None and self._expr.plan is not None:
            return self._expr.plan
        return Plan(source=Source(kind="context", root=type(self).__name__))

    def _respawn(self, plan: Plan, returns: str | None = None) -> Any:
        """A new lazy wrapper of `returns` (or this class) carrying `plan`."""
        if returns in (None, "self"):
            cls = type(self)
        else:
            cls = LAZY_TYPES.get(returns, type(self))
        obj = cls.__new__(cls)                       # type: ignore[call-overload]
        object.__setattr__(obj, "_expr", ExprState(plan=plan))
        obj._init_lazy()
        return obj

    def _init_lazy(self) -> None:
        """Null out eager state on a freshly spawned lazy wrapper."""

    # -- naming -------------------------------------------------------------
    def alias(self, name: str) -> Self:
        """Name this expression's column. Needed for positional fields in
        `then`/`map`; keyword fields are named by their keyword."""
        clone = self._clone()
        state = clone._expr
        object.__setattr__(
            clone, "_expr",
            ExprState(plan=state.plan if state else None, name=name))
        return clone

    def _clone(self) -> Self:
        obj = type(self).__new__(type(self))         # type: ignore[call-overload]
        for slot, value in self.__dict__.items():
            object.__setattr__(obj, slot, value)
        return obj                                    # type: ignore[return-value]

    # -- record constructors -------------------------------------------------
    def then(self, *positional: "Expr", **named: "Expr") -> Any:
        """One context in, one record out. Evaluates now if this wrapper is
        eager; records otherwise."""
        return self._project(ThenStep(fields=_projections(positional, named)))

    def map(self, *positional: "Expr", **named: "Expr") -> Any:
        """`then` lifted over a collection: many contexts in, many records
        out."""
        return self._project(MapStep(fields=_projections(positional, named)))

    def filter(self, predicate: "Expr") -> Any:
        return self._project(FilterStep(predicate=predicate._plan_or_new()))

    def explode(self, path: str) -> Any:
        return self._project(ExplodeStep(path=path))

    def limit(self, count: int) -> Any:
        return self._project(LimitStep(count=count))

    def otherwise(self, *sentinel: Any, **recovery: "Expr") -> Any:
        """Recover from any failure recorded before this point: either a
        sentinel (`RAISE_ERROR` / `DROP_ROW` / `NULL`) or a projection
        evaluated against the failure, with `doc` and `err` in scope."""
        if sentinel and recovery:
            raise PlanError("otherwise() takes a sentinel or a recovery "
                            "projection, not both")
        if sentinel:
            if len(sentinel) != 1 or not isinstance(sentinel[0], _Sentinel):
                raise PlanError(
                    "otherwise() takes RAISE_ERROR, DROP_ROW or NULL, "
                    f"got {sentinel!r}")
            step = OtherwiseStep(sentinel=sentinel[0].policy)
        else:
            step = OtherwiseStep(recovery=_projections((), recovery))
        return self._project(step)

    def _project(self, step: Any) -> Any:
        plan = self._plan_or_new().extend(step)
        if self.is_lazy:
            return self._respawn(plan, _RESULT_OF.get(step.kind))
        from .execute import evaluate_now
        return evaluate_now(plan, self)

    # -- terminals ----------------------------------------------------------
    def to_plan(self) -> Plan:
        return self._plan_or_new()

    def explain(self) -> str:
        from .explain import explain_plan
        return explain_plan(self._plan_or_new())

    def collect(self, context: Any = None, *, client: Any = None) -> Any:
        """Run this plan. A rooted plan needs no context."""
        from .execute import collect_plan
        return collect_plan(self._plan_or_new(), context, client=client)

    # -- operators ----------------------------------------------------------
    def _binop(self, operator: str, other: Any = None) -> Any:
        if self.is_lazy:
            return self._respawn(
                self._plan_or_new().extend(
                    BinOpStep(operator=operator, right=_to_arg(other))),
                "Value")
        return _EAGER_OPS[operator](self._eager_value(), _unwrap(other))

    def _eager_value(self) -> Any:
        raise NotImplementedError

    # `==` gives a bool eagerly and an expression lazily. That asymmetry is
    # inherent to one class in two modes (pandas and SQLAlchemy carry it too);
    # `Any` is the honest annotation.
    def __eq__(self, other: Any) -> Any:   # type: ignore[override]
        return self._binop("eq", other)

    def __ne__(self, other: Any) -> Any:   # type: ignore[override]
        return self._binop("ne", other)

    def __lt__(self, other: Any) -> Any:
        return self._binop("lt", other)

    def __le__(self, other: Any) -> Any:
        return self._binop("le", other)

    def __gt__(self, other: Any) -> Any:
        return self._binop("gt", other)

    def __ge__(self, other: Any) -> Any:
        return self._binop("ge", other)

    def __and__(self, other: Any) -> Any:
        return self._binop("and", other)

    def __or__(self, other: Any) -> Any:
        return self._binop("or", other)

    def __invert__(self) -> Any:
        return self._binop("not")

    def __hash__(self) -> int:
        if self.is_lazy:
            raise TypeError(
                "a lazy expression is not hashable (it has no value yet)")
        return hash(self._eager_value())


_RESULT_OF: dict[str, str] = {
    "then": "Record", "map": "RecordSet", "filter": "Selection",
    "explode": "RecordSet", "limit": "Selection", "otherwise": "Record",
}

_EAGER_OPS: dict[str, Callable[[Any, Any], Any]] = {
    "eq": lambda a, b: a == b, "ne": lambda a, b: a != b,
    "lt": lambda a, b: a < b, "le": lambda a, b: a <= b,
    "gt": lambda a, b: a > b, "ge": lambda a, b: a >= b,
    "and": lambda a, b: bool(a) and bool(b),
    "or": lambda a, b: bool(a) or bool(b),
    "not": lambda a, _: not a,
    "contains": lambda a, b: b in a,
}


def _unwrap(value: Any) -> Any:
    return value.get() if isinstance(value, (Value, Selection)) else value


def _to_arg(value: Any) -> Arg:
    if isinstance(value, Expr) and value.is_lazy:
        return Arg(kind="plan", plan=value._plan_or_new())
    return Arg(kind="literal", value=_unwrap(value))


def _projections(positional: Sequence["Expr"],
                 named: Mapping[str, "Expr"]) -> list[Projection]:
    fields: list[Projection] = []
    for index, expr in enumerate(positional):
        name = expr._column_name if isinstance(expr, Expr) else None
        if not name:
            raise PlanError(
                f"positional field #{index} has no name — add .alias(\"...\") "
                "or pass it as a keyword argument")
        fields.append(Projection(name=name, plan=expr._plan_or_new()))
    for name, expr in named.items():
        if not isinstance(expr, Expr):
            from .plan import LiteralStep
            fields.append(Projection(
                name=name, plan=Plan(steps=[LiteralStep(value=expr)])))
        else:
            fields.append(Projection(name=name, plan=expr._plan_or_new()))
    seen: set[str] = set()
    for field in fields:
        if field.name in seen:
            raise PlanError(f"duplicate field name {field.name!r}")
        seen.add(field.name)
    return fields


# --------------------------------------------------------------------------- #
# Value / Selection
# --------------------------------------------------------------------------- #

class Value(Expr, Generic[T]):
    """A scalar result. Eagerly materialised, lazily a plan."""

    def __init__(self, value: T | None = None) -> None:
        self._value = value
        self._expr = None

    def _init_lazy(self) -> None:
        self._value = None

    def _eager_value(self) -> Any:
        return self._value

    def get(self) -> T:
        """The underlying value. Raises if this wrapper is unevaluated."""
        if self.is_lazy:
            raise WebClientError(
                "this Value is a lazy expression — call .collect() to run it")
        return self._value            # type: ignore[return-value]

    def __str__(self) -> str:
        return "" if self._value is None else str(self._value)

    def __format__(self, spec: str) -> str:
        return format(self._value, spec)

    def __bool__(self) -> bool:
        return bool(self._value)

    def __len__(self) -> int:
        return len(self._value)       # type: ignore[arg-type]

    def __contains__(self, item: Any) -> bool:
        return item in self._value    # type: ignore[operator]

    def __repr__(self) -> str:
        if self.is_lazy:
            return f"Value(<lazy {len(self._plan_or_new().steps)} steps>)"
        return f"Value({self._value!r})"

    # A lazy wrapper forwards unknown names to the op registry: `field("link")`
    # yields a Value that must accept `.resolve()`, and a column's type is a
    # run-time fact. The name is validated against the registry as it is
    # recorded, so a typo fails here rather than at collect time.
    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        from .ops import REGISTRY
        if self.is_lazy:
            spec = REGISTRY.any_named(name)
            if spec is not None:
                return _forward(self, spec)
            raise AttributeError(
                f"no op named {name!r} (known: {', '.join(REGISTRY.names())})")
        raise AttributeError(
            f"{type(self).__name__} has no attribute {name!r}; this wrapper "
            "is already evaluated — use .get() for the value")



class Selection(Expr, Generic[T]):
    """An ordered collection result — what `select_all` returns."""

    def __init__(self, items: Sequence[T] | None = None) -> None:
        self._items: list[T] = list(items or ())
        self._expr = None

    def _init_lazy(self) -> None:
        self._items = []

    def _eager_value(self) -> Any:
        return self._items

    def get(self) -> list[T]:
        if self.is_lazy:
            raise WebClientError(
                "this Selection is a lazy expression — call .collect()")
        return list(self._items)

    def __iter__(self) -> Iterator[T]:
        return iter(self.get())

    def __len__(self) -> int:
        return len(self.get())

    @overload
    def __getitem__(self, index: int) -> T: ...
    @overload
    def __getitem__(self, index: slice) -> list[T]: ...
    def __getitem__(self, index: int | slice) -> Any:
        return self.get()[index]

    def __bool__(self) -> bool:
        return bool(self._items)

    def __repr__(self) -> str:
        if self.is_lazy:
            return f"Selection(<lazy {len(self._plan_or_new().steps)} steps>)"
        return f"Selection({self._items!r})"

    # A lazy wrapper forwards unknown names to the op registry: `field("link")`
    # yields a Value that must accept `.resolve()`, and a column's type is a
    # run-time fact. The name is validated against the registry as it is
    # recorded, so a typo fails here rather than at collect time.
    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        from .ops import REGISTRY
        if self.is_lazy:
            spec = REGISTRY.any_named(name)
            if spec is not None:
                return _forward(self, spec)
            raise AttributeError(
                f"no op named {name!r} (known: {', '.join(REGISTRY.names())})")
        raise AttributeError(
            f"{type(self).__name__} has no attribute {name!r}; this wrapper "
            "is already evaluated — use .get() for the value")



def _forward(wrapper: "Expr", spec: Any) -> Any:
    """Record a call to `spec` against a lazy wrapper."""
    from .plan import CallStep

    def record(*args: Any, **kwargs: Any) -> Any:
        step = CallStep(op=spec.name, args=[_to_arg(a) for a in args],
                        kwargs={k: _to_arg(v) for k, v in kwargs.items()})
        returns = (spec.returns_for(args, kwargs) if spec.returns_for
                   else spec.returns)
        return wrapper._respawn(wrapper._plan_or_new().extend(step), returns)

    return record() if spec.is_property else record


register_lazy_type("Value", Value)
register_lazy_type("Selection", Selection)
