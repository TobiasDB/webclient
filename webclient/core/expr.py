"""The expression language: ``Expr``, ``lazy()``, the plan IR.

``Expr`` is independent of the models (spec.py): it records attribute
access, calls and operators into a ``Plan`` and knows nothing about
Document, Reference or Collection. Static types come from ``lazy(cls) ->
T``; at runtime every value in a chain is an ``Expr``. ``lazy(cls)``
registers ``cls`` as a plan root -- what a plan from the wire is validated
against, together with the rule that no recorded name starts with ``_``
(the whole safety model: the ``__subclasses__`` escape needs a dunder).

Return types are for validation only and are not needed to record. The
seams for that (Self / unions / overloads) are deferred; recording works
without them.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal, TypeVar, cast

from pydantic import BaseModel

T = TypeVar("T")

LAZY_TYPES: dict[str, type] = {}
#: operators recordable as steps (the dunders below map onto these names)
OPERATORS = {"eq", "ne", "lt", "le", "gt", "ge", "and", "or", "not"}
#: free functions recordable as steps (spec.py: is_empty / is_ok / ...)
FUNCTIONS = {"is_empty", "is_ok"}


class Arg(BaseModel):
    value: Any = None
    plan: Plan | None = None         # a sub-expression, bound to the context


class Step(BaseModel):
    kind: Literal["get", "call", "op", "fn", "when"]
    name: str = ""                   # attribute / operator / function name
    args: list[Arg] = []
    kwargs: dict[str, Arg] = {}


class Plan(BaseModel):
    """A root type name ('' = the evaluation context itself), an optional
    source (the request spec a ``Reference`` root starts from), an optional
    session id (binds resolution to a session), and steps."""

    version: int = 1
    root: str = ""
    source: dict[str, Any] | None = None
    session_id: str | None = None
    steps: list[Step] = []

    def extend(self, step: Step) -> Plan:
        return self.model_copy(update={"steps": [*self.steps, step]})

    def validate_names(self) -> Plan:
        """Reject a plan that could not have been recorded: an unknown root,
        a private name, an unknown operator/function. Any *public* method
        name is allowed -- the executor calls it by getattr (Decision: no
        whitelist; ``_``-refusal is the safety boundary)."""
        if self.root and self.root not in LAZY_TYPES:
            raise ValueError(f"unknown plan root {self.root!r}")
        for step in self.steps:
            if step.name.startswith("_"):
                raise ValueError(f"private name {step.name!r} in plan")
            if step.kind == "op" and step.name not in OPERATORS:
                raise ValueError(f"unknown operator {step.name!r} in plan")
            if step.kind == "fn" and step.name not in FUNCTIONS:
                raise ValueError(f"unknown function {step.name!r} in plan")
            for arg in (*step.args, *step.kwargs.values()):
                if arg.plan is not None:
                    arg.plan.validate_names()
        return self

    def describe(self) -> str:
        out = f"{self.root}({self.source.get('hostname', '')})" if self.source \
            else (self.root or "·")
        for s in self.steps:
            args = ", ".join([*map(_show, s.args),
                              *(f"{k}={_show(v)}" for k, v in s.kwargs.items())])
            out = {"get": f"{out}.{s.name}", "call": f"{out}({args})",
                   "op": f"({out} {s.name} {args})",
                   "fn": f"{s.name}({out}{', ' + args if args else ''})",
                   "when": f"when({args})"}[s.kind]
        return out


def _show(arg: Arg) -> str:
    return arg.plan.describe() if arg.plan is not None else repr(arg.value)


Arg.model_rebuild()


class _Missing:
    pass


_MISSING: Any = _Missing()


class Expr:
    """A recorded chain. Every attribute access, call or operator returns a
    new ``Expr`` extending the plan; nothing runs until the evaluator walks
    it (or an eager ``extract``/``filter`` binds it to a real object)."""

    __slots__ = ("_plan", "_client")

    def __init__(self, plan: Plan, client: Any = None) -> None:
        object.__setattr__(self, "_plan", plan)
        object.__setattr__(self, "_client", client)

    # -- recording -----------------------------------------------------------
    def _extend(self, step: Step) -> Expr:
        return Expr(self._plan.extend(step), self._client)

    def __getattr__(self, name: str) -> Expr:
        if name.startswith("_"):
            raise AttributeError(name)
        return self._extend(Step(kind="get", name=name))

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        # per-call eager (PLAN §8): op(..., _collect=True) records then collects
        # immediately through the one evaluation path. Otherwise stays lazy.
        eager = kwargs.pop("_collect", False)
        nxt = self._extend(Step(kind="call", args=[to_arg(a) for a in args],
                                kwargs={k: to_arg(v) for k, v in kwargs.items()}))
        return nxt.collect() if eager else nxt

    def _op(self, name: str, other: Any = _MISSING) -> Expr:
        args = [] if other is _MISSING else [to_arg(other)]
        return self._extend(Step(kind="op", name=name, args=args))

    def __eq__(self, o: Any) -> Expr: return self._op("eq", o)   # type: ignore[override]
    def __ne__(self, o: Any) -> Expr: return self._op("ne", o)   # type: ignore[override]
    def __lt__(self, o: Any) -> Expr: return self._op("lt", o)
    def __le__(self, o: Any) -> Expr: return self._op("le", o)
    def __gt__(self, o: Any) -> Expr: return self._op("gt", o)
    def __ge__(self, o: Any) -> Expr: return self._op("ge", o)
    def __and__(self, o: Any) -> Expr: return self._op("and", o)
    def __or__(self, o: Any) -> Expr: return self._op("or", o)
    def __invert__(self) -> Expr: return self._op("not")

    __hash__ = None  # type: ignore[assignment]

    def _coerce(self, what: str) -> Any:
        raise TypeError(
            f"a lazy expression has no {what}: it records, it does not run. "
            "Use it inside extract(...) / filter(...), is_empty()/is_ok(), "
            "or `& | ~` -- not and/or/not/bool/len/iter.")

    def __bool__(self) -> bool: return self._coerce("truth value")
    def __len__(self) -> int: return self._coerce("length")
    def __iter__(self) -> Any: return self._coerce("iterator")

    # -- evaluation ----------------------------------------------------------
    def collect(self, context: Any = None) -> Any:
        """Evaluate this recorded plan and return the result -- the single lazy
        trigger (full-lazy migration, PLAN §8). Runs on the bound client's core
        (or the process default) via its engine loop. ``collect`` is a reserved,
        non-recordable name (like a ``_``-prefixed one)."""
        from .engine import default_client
        client = self._client
        if client is None:
            client = default_client().core
        loop = client._ensure_loop()
        return loop.run(client.execute(self, context))

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
    """A recording root for ``cls``: statically ``cls``, at runtime an
    ``Expr``. Registers ``cls`` as a valid plan root."""
    origin = getattr(cls, "__pydantic_generic_metadata__", {}).get("origin") or cls
    LAZY_TYPES[origin.__name__] = origin
    return cast(T, Expr(plan or Plan(root=origin.__name__), client))


def from_plan(plan: Plan | dict[str, Any], client: Any = None) -> Expr:
    """Rebuild an ``Expr`` from its wire form; validates names first."""
    if isinstance(plan, dict):
        plan = Plan.model_validate(plan)
    plan.validate_names()
    return Expr(plan, client)


# --------------------------------------------------------------------------- #
# Roots and free functions
# --------------------------------------------------------------------------- #

def _install_roots() -> None:
    """Expose the typed roots and register the root types. Called at the end
    of models.py, keeping the import edge one-way (models import expr)."""
    global doc, many, ref
    from .document import Collection, Document, Field, Reference
    LAZY_TYPES["Field"] = Field
    doc = lazy(Document)
    many = lazy(cast(type, Collection))
    ref = lazy(Reference)


if TYPE_CHECKING:
    from .base import Field
    from .document import Collection, Document, Reference
    doc: Document
    many: Collection[Document]
    ref: Reference
else:
    doc = many = ref = None


def field(name: str) -> Any:
    """A value already extracted in this context (Decision 12)."""
    return doc.field(name)


def _fn(name: str, expr: Any) -> Expr:
    base = expr if isinstance(expr, Expr) else Expr(Plan())
    return base._extend(Step(kind="fn", name=name))


def is_empty(expr: Any) -> Any:
    return _fn("is_empty", expr)


def is_ok(expr: Any) -> Any:
    return _fn("is_ok", expr)


class _When:
    """Polars-style branching builder: ``when(cond).then(a).otherwise(b)`` --
    a free construct, not a ``Field`` method. Records a single ``when`` step
    whose condition / then / else are sub-expressions evaluated against the
    surrounding context (e.g. per element inside ``extract``)."""

    __slots__ = ("_cond", "_then")

    def __init__(self, cond: Any) -> None:
        self._cond = cond
        self._then: Any = _MISSING

    def then(self, value: Any) -> "_When":
        self._then = value
        return self

    def otherwise(self, value: Any) -> "Field[Any]":
        if self._then is _MISSING:
            raise TypeError("when(...).then(...) is required before .otherwise(...)")
        step = Step(kind="when", args=[to_arg(self._cond), to_arg(self._then),
                                       to_arg(value)])
        return cast("Field[Any]", Expr(Plan(steps=[step])))   # runtime: an Expr


def when(cond: Any) -> _When:
    """Start a Polars-style conditional: ``when(cond).then(a).otherwise(b)``."""
    return _When(cond)


def filter(collection: Any, *predicates: Any) -> Any:
    """Free-function form of the collection filter (your ``wc.filter``):
    ``filter(coll, pred)`` == ``coll.filter(pred)``."""
    return collection.filter(*predicates)


def reference(url: str, **kwargs: Any) -> "Reference":
    """A lazy reference root starting from ``url`` (construction lives here, not
    in ``Reference.__new__`` -- PLAN §9). Statically a ``Reference``; at runtime
    an ``Expr`` recording a plan rooted at that request spec."""
    from .document import Reference
    spec = Reference.from_url(url, **kwargs).request_fields()
    return lazy(Reference, plan=Plan(root="Reference", source=spec))


__all__ = ["Arg", "Step", "Plan", "Expr", "lazy", "from_plan", "to_arg",
           "doc", "many", "ref", "reference", "field", "is_empty", "is_ok",
           "when", "filter", "LAZY_TYPES", "OPERATORS", "FUNCTIONS"]
