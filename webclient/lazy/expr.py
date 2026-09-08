"""Lazy expression DSL (polars-style) -- the recorder.

Every attribute access or call on a lazy value records an op and returns
another Expr; nothing executes until ``collect`` (the Executor, M6). A
recorded pipeline serializes to a JSON ``QueryPlan`` -- the wire format for
the eventual HTTP/websocket API. Completion in a REPL comes from ``__dir__``
delegating to the wrapped eager class; record-time validation catches typos
that ``Expr.__getattr__`` typing cannot.
"""
from __future__ import annotations

import inspect
import typing
from typing import Any, Callable

from pydantic import BaseModel

from ..models import Document, Node, Reference

# Combinator / control names handled by Expr itself (never recorded as ops
# against the wrapped eager class).
_COMBINATORS = {"map", "filter", "then", "otherwise"}
_TERMINALS = {"collect", "to_query", "explain"}
_DUNDER_PASSTHROUGH = {"as_expr"}

# Eager classes a chain can be rooted at / transition through, by name.
_TYPES: dict[str, type] = {}


def _register_types() -> None:
    from ..live import LiveDocument, LiveNode
    _TYPES.update(Reference=Reference, Document=Document, Node=Node,
                  LiveDocument=LiveDocument, LiveNode=LiveNode)


class QueryPlan(BaseModel):
    """Serialized form of an Expr: the query-syntax / wire representation."""

    version: int = 1
    root: str | None = None          # root context type name, if known
    steps: list[dict[str, Any]] = []


class _Return(typing.NamedTuple):
    cls: type | None                 # resolved element/return class, or None
    many: bool                       # True if the method returns a collection


def _resolve_return(owner: type, method_name: str) -> _Return:
    """Best-effort return type of ``owner.method_name`` from its annotation,
    for validation and to know what element type ``map`` iterates. Overloaded
    stubs annotate the implementation; unions collapse to the non-None arm."""
    if not _TYPES:
        _register_types()
    member = getattr(owner, method_name, None)
    if member is None:
        return _Return(None, False)
    if isinstance(member, property):
        func: Any = member.fget
    else:
        func = member
    try:
        hints = typing.get_type_hints(func)
    except Exception:
        return _Return(None, False)
    annotation = hints.get("return")
    if annotation is None:
        return _Return(None, False)
    origin = typing.get_origin(annotation)
    args = typing.get_args(annotation)
    if origin in (list, typing.Sequence) or str(origin).endswith("Sequence"):
        inner = args[0] if args else None
        return _Return(_as_type(inner), True)
    if origin is typing.Union:
        non_none = [a for a in args if a is not type(None)]
        if len(non_none) == 1:
            return _Return(_as_type(non_none[0]), False)
        return _Return(None, False)
    return _Return(_as_type(annotation), False)


def _as_type(annotation: Any) -> type | None:
    if isinstance(annotation, type) and annotation in _TYPES.values():
        return annotation
    name = getattr(annotation, "__name__", None)
    return _TYPES.get(name) if name else None


class Expr:
    """A recorded chain. Immutable: every op returns a new Expr."""

    __slots__ = ("_steps", "_type", "_many", "_root")

    def __init__(self, steps: list[dict[str, Any]], type_: type | None,
                 many: bool = False, root: str | None = None) -> None:
        self._steps = steps
        self._type = type_            # current known class (None = unknown)
        self._many = many             # current value is a collection
        self._root = root

    # -- recording -----------------------------------------------------------
    def _extend(self, step: dict[str, Any], type_: type | None,
                many: bool) -> Expr:
        return Expr(self._steps + [step], type_, many, self._root)

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        if name in _COMBINATORS or name in _TERMINALS:
            return _BoundCombinator(self, name)
        # Validate against the current known class (element class if we're on
        # a collection -- attribute access maps over it).
        owner = self._type
        if owner is not None and not hasattr(owner, name):
            raise AttributeError(
                f"{owner.__name__} has no attribute {name!r} "
                f"(recording a lazy chain)")
        ret = _resolve_return(owner, name) if owner is not None else _Return(None, False)
        return _Access(self, name, ret)

    def __call__(self, *args: Any, **kwargs: Any) -> Expr:
        raise TypeError("this Expr is not callable; call a method on it")

    # -- comparisons / logic -> boolean Expr ---------------------------------
    def _binop(self, op: str, other: Any) -> Expr:
        value = other._steps if isinstance(other, Expr) else other
        is_expr = isinstance(other, Expr)
        return self._extend(
            {"binop": op, "value": value, "value_is_expr": is_expr},
            bool, False)

    def __eq__(self, other: Any) -> Expr:  # type: ignore[override]
        return self._binop("eq", other)

    def __ne__(self, other: Any) -> Expr:  # type: ignore[override]
        return self._binop("ne", other)

    def __lt__(self, other: Any) -> Expr:
        return self._binop("lt", other)

    def __gt__(self, other: Any) -> Expr:
        return self._binop("gt", other)

    def __and__(self, other: Any) -> Expr:
        return self._binop("and", other)

    def __or__(self, other: Any) -> Expr:
        return self._binop("or", other)

    def __invert__(self) -> Expr:
        return self._extend({"binop": "not"}, bool, False)

    __hash__ = None  # type: ignore[assignment]

    # -- combinators (recorded as structured steps) --------------------------
    def _record_combinator(self, name: str, args: tuple,
                           kwargs: dict) -> Expr:
        if name == "map":
            fields = {k: _to_steps(v) for k, v in kwargs.items()}
            # map iterates the current collection; fields see the element type
            return self._extend(
                {"op": "map", "fields": fields}, dict, True)
        if name == "then":
            fields = {k: _to_steps(v) for k, v in kwargs.items()}
            return self._extend({"op": "then", "fields": fields},
                                self._type, self._many)
        if name == "filter":
            predicate = _to_steps(args[0])
            return self._extend({"op": "filter", "predicate": predicate},
                                self._type, self._many)
        if name == "otherwise":
            state = args[0]
            value = state.value if hasattr(state, "value") else state
            return self._extend({"op": "otherwise", "state": value},
                                self._type, self._many)
        raise AssertionError(name)

    # -- terminals -----------------------------------------------------------
    def to_query(self) -> QueryPlan:
        return QueryPlan(root=self._root, steps=list(self._steps))

    @classmethod
    def from_query(cls, plan: "QueryPlan | dict[str, Any]") -> Expr:
        if isinstance(plan, dict):
            plan = QueryPlan(**plan)
        root_cls = _TYPES.get(plan.root) if plan.root else None
        return cls(list(plan.steps), root_cls, False, plan.root)

    def explain(self) -> str:
        lines = [f"root: {self._root or '?'}"]
        for step in self._steps:
            lines.append("  " + _explain_step(step))
        return "\n".join(lines)

    def collect(self, context: Any, *, stream: bool = False,
                client: Any = None) -> Any:
        from ..models import Reference as _Ref
        wc = client
        if wc is None:
            bound = getattr(context, "bound", None) or getattr(
                context, "_client", None)
            if bound is not None:
                wc = bound
            else:
                from ..client import default_client
                wc = default_client()
        return wc.execute(self, context, stream=stream)

    def __repr__(self) -> str:
        return f"Expr({self._root or '?'}, {len(self._steps)} steps)"


class _Access:
    """A recorded-but-not-yet-called attribute. Acts as the attribute value
    (property access) if used directly, or records a method call if
    invoked."""

    __slots__ = ("_expr", "_name", "_ret")

    def __init__(self, expr: Expr, name: str, ret: _Return) -> None:
        self._expr = expr
        self._name = name
        self._ret = ret

    def __call__(self, *args: Any, **kwargs: Any) -> Expr:
        s_args = [_arg(a) for a in args]
        s_kwargs = {k: _arg(v) for k, v in kwargs.items()}
        return self._expr._extend(
            {"op": "call", "name": self._name, "args": s_args,
             "kwargs": s_kwargs},
            self._ret.cls, self._ret.many)

    def _as_expr(self) -> Expr:
        # property / attribute access, e.g. `.text`
        return self._expr._extend(
            {"op": "get", "name": self._name}, self._ret.cls, self._ret.many)

    # make property access chain-able and comparable transparently
    def __getattr__(self, name: str) -> Any:
        return getattr(self._as_expr(), name)

    def __eq__(self, other: Any) -> Expr:  # type: ignore[override]
        return self._as_expr() == other

    def __ne__(self, other: Any) -> Expr:  # type: ignore[override]
        return self._as_expr() != other

    __hash__ = None  # type: ignore[assignment]


class _BoundCombinator:
    __slots__ = ("_expr", "_name")

    def __init__(self, expr: Expr, name: str) -> None:
        self._expr = expr
        self._name = name

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        if self._name in _TERMINALS:
            return getattr(Expr, self._name)(self._expr, *args, **kwargs)
        return self._expr._record_combinator(self._name, args, kwargs)


def _arg(value: Any) -> Any:
    if isinstance(value, Expr):
        return {"__expr__": value._steps}
    if isinstance(value, _Access):
        return {"__expr__": value._as_expr()._steps}
    return value


def _to_steps(value: Any) -> Any:
    if isinstance(value, Expr):
        return {"root": value._root, "steps": value._steps}
    if isinstance(value, _Access):
        e = value._as_expr()
        return {"root": e._root, "steps": e._steps}
    return {"literal": value}


def _explain_step(step: dict[str, Any]) -> str:
    if "op" in step:
        if step["op"] == "call":
            return f".{step['name']}(...)"
        if step["op"] == "get":
            return f".{step['name']}"
        if step["op"] == "map":
            return f"map({', '.join(step['fields'])})"
        return step["op"]
    if "binop" in step:
        return f"{step['binop']} {step.get('value', '')}"
    return str(step)


# --------------------------------------------------------------------------- #
# Lazy proxy + q namespace
# --------------------------------------------------------------------------- #

class Lazy:
    """Chain entry point: wraps an eager class so attribute access starts
    recording. The wrapped class is the plan's root context type."""

    __slots__ = ("_cls",)

    def __init__(self, cls: type) -> None:
        self._cls = cls

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        root = Expr([], self._cls, False, self._cls.__name__)
        return getattr(root, name)

    def __dir__(self) -> list[str]:
        return dir(self._cls)


def col(name: str) -> Expr:
    """Reference a field produced earlier in the pipeline."""
    return Expr([{"op": "col", "name": name}], None, False)


def lit(value: Any) -> Expr:
    """Wrap a literal value as an Expr."""
    return Expr([{"op": "lit", "value": value}], None, False)


class q:
    """The lazy namespace (polars style)."""

    ref = Lazy(Reference)
    doc = Lazy(Document)
    node = Lazy(Node)
    col = staticmethod(col)
    lit = staticmethod(lit)

    def __init__(self) -> None:  # pragma: no cover - namespace, not instantiated
        raise TypeError("q is a namespace, not a type")


def _install_live_proxies() -> None:
    """live / live_node reference LiveDocument/LiveNode, imported lazily to
    avoid a cycle (live.py imports models, not the lazy layer)."""
    from ..live import LiveDocument, LiveNode
    q.live = Lazy(LiveDocument)        # type: ignore[attr-defined]
    q.live_node = Lazy(LiveNode)       # type: ignore[attr-defined]


_install_live_proxies()
