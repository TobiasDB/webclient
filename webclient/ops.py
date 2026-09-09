"""`@op` — one declaration, two evaluation modes.

Every surface method is declared once. The decorator records metadata the
scheduler and validator read (purity, capability, resource, cardinality),
then wraps the implementation in a single branch:

    if self.is_lazy:  record a CallStep and return a lazy wrapper
    else:             check capability, call the implementation

There is no second implementation of any op anywhere, which is what stops
recording and evaluation from drifting.

The decorators are *typed* as identity so a type checker keeps seeing the
declared signature and return type; only the runtime object differs.
"""
from __future__ import annotations

import functools
import inspect
from dataclasses import dataclass, field
from typing import Any, Callable, TypeVar, cast

from .errors import UnsupportedOperation
from .plan import Arg, CallStep, Plan

F = TypeVar("F", bound=Callable[..., Any])
R = TypeVar("R")

Cardinality = str  # "one->one" | "one->many" | "many->one" | "many->many"


@dataclass
class OpSpec:
    """Everything the rest of the system needs to know about an op."""

    name: str
    fn: Callable[..., Any]
    owner: str = ""
    pure: bool = False
    capability: str | None = None
    resource: str | None = None
    returns: str = "Value"
    cardinality: Cardinality = "one->one"
    mutates: bool = False
    is_property: bool = False
    hint: str | None = None
    #: for ops whose result class depends on their arguments (`attr("href")`
    #: narrows to a Reference, `attr("text")` does not)
    returns_for: Callable[[tuple[Any, ...], dict[str, Any]], str] | None = None
    signature: inspect.Signature = field(default_factory=lambda: inspect.Signature())

    @property
    def key(self) -> str:
        return f"{self.owner}.{self.name}" if self.owner else self.name


class OpRegistry:
    """Every declared op, by owner and by bare name.

    Bare-name lookup is what the evaluator uses: a plan says `attr`, and the
    receiver at run time decides whose `attr` that is.
    """

    def __init__(self) -> None:
        self._by_key: dict[str, OpSpec] = {}
        self._by_name: dict[str, list[OpSpec]] = {}

    def register(self, spec: OpSpec) -> None:
        self._by_key[spec.key] = spec
        self._by_name.setdefault(spec.name, []).append(spec)

    def lookup(self, owner: str, name: str) -> OpSpec | None:
        return self._by_key.get(f"{owner}.{name}")

    def any_named(self, name: str) -> OpSpec | None:
        found = self._by_name.get(name)
        return found[0] if found else None

    def known(self, name: str) -> bool:
        return name in self._by_name

    def names(self) -> list[str]:
        return sorted(self._by_name)

    def all(self) -> list[OpSpec]:
        return list(self._by_key.values())


REGISTRY = OpRegistry()


# --------------------------------------------------------------------------- #
# Shared machinery
# --------------------------------------------------------------------------- #

def _record(obj: Any, spec: OpSpec, args: tuple[Any, ...],
            kwargs: dict[str, Any]) -> Any:
    from .values import _to_arg
    step = CallStep(
        op=spec.name,
        args=[_to_arg(a) for a in args],
        kwargs={k: _to_arg(v) for k, v in kwargs.items()},
    )
    plan: Plan = obj._plan_or_new().extend(step)
    returns = spec.returns_for(args, kwargs) if spec.returns_for else spec.returns
    return obj._respawn(plan, returns)


def _check_capability(obj: Any, spec: OpSpec) -> None:
    if spec.capability is None:
        return
    capabilities = getattr(obj, "_capabilities", frozenset())
    if spec.capability in capabilities:
        return
    handler = getattr(obj, "_capability_error", None)
    if handler is not None:
        raise handler(spec)
    raise UnsupportedOperation(spec.name, spec.capability,
                               getattr(obj, "_backing_name", "unknown"),
                               spec.hint)


def _settle(obj: Any, result: Any) -> Any:
    """Run an op implementation's coroutine on the engine, if it returned
    one. Pure ops never do, so they cost no thread hop."""
    if inspect.iscoroutine(result):
        from .engine import run_sync
        return run_sync(result)
    return result


def _make_spec(fn: Callable[..., Any], meta: dict[str, Any],
               is_property: bool) -> OpSpec:
    return OpSpec(name=fn.__name__, fn=fn, is_property=is_property,
                  signature=inspect.signature(fn), **meta)


# --------------------------------------------------------------------------- #
# Decorators
# --------------------------------------------------------------------------- #

def op(*, pure: bool = False, capability: str | None = None,
       resource: str | None = None, returns: str = "Value",
       cardinality: Cardinality = "one->one", mutates: bool = False,
       hint: str | None = None,
       returns_for: Callable[..., str] | None = None) -> Callable[[F], F]:
    """Declare a surface method as an op.

    `pure` ops run inline and take no lease. `capability` is checked against
    the receiver's backing at call time — capability is runtime state, so a
    Document can lose one. `returns` names the wrapper class produced in lazy
    mode. `cardinality` is what recording and evaluation both read, so
    element-wise mapping is one declared fact rather than two assumptions.
    """
    meta = dict(pure=pure, capability=capability, resource=resource,
                returns=returns, cardinality=cardinality, mutates=mutates,
                hint=hint, returns_for=returns_for)

    def decorate(fn: F) -> F:
        spec = _make_spec(cast(Callable[..., Any], fn), meta, False)

        @functools.wraps(cast(Callable[..., Any], fn))
        def invoke(self: Any, *args: Any, **kwargs: Any) -> Any:
            if getattr(self, "is_lazy", False):
                return _record(self, spec, args, kwargs)
            _check_capability(self, spec)
            return _settle(self, spec.fn(self, *args, **kwargs))

        invoke.__opspec__ = spec                     # type: ignore[attr-defined]
        _pending.append(spec)
        return cast(F, invoke)

    return decorate


def op_property(*, pure: bool = True, capability: str | None = None,
                returns: str = "Value", hint: str | None = None
                ) -> Callable[[Callable[[Any], R]], R]:
    """An op with no arguments, exposed as a property. Records in lazy mode
    exactly as a call with no arguments."""
    meta = dict(pure=pure, capability=capability, resource=None,
                returns=returns, cardinality="one->one", mutates=False,
                hint=hint, returns_for=None)

    def decorate(fn: Callable[[Any], R]) -> R:
        spec = _make_spec(fn, meta, True)

        @functools.wraps(fn)
        def getter(self: Any) -> Any:
            if getattr(self, "is_lazy", False):
                return _record(self, spec, (), {})
            _check_capability(self, spec)
            return _settle(self, spec.fn(self))

        _pending.append(spec)
        return cast(R, property(getter, doc=fn.__doc__))

    return decorate


#: Specs declared before their owning class exists; `bind_ops` claims them.
_pending: list[OpSpec] = []


def bind_ops(cls: type) -> type:
    """Class decorator: give every op declared in this class body its owner
    and put it in the registry."""
    owned = {name for name, value in vars(cls).items()}
    for spec in list(_pending):
        if spec.name in owned:
            spec.owner = cls.__name__
            REGISTRY.register(spec)
            _pending.remove(spec)
    return cls


def ops_of(cls: type) -> list[OpSpec]:
    return [s for s in REGISTRY.all() if s.owner == cls.__name__]


# --------------------------------------------------------------------------- #
# `.core` — the async pass-through
# --------------------------------------------------------------------------- #

def _spec_for(obj: Any, name: str) -> OpSpec:
    for klass in type(obj).__mro__:
        spec = REGISTRY.lookup(klass.__name__, name)
        if spec is not None:
            return spec
    raise AttributeError(
        f"{type(obj).__name__!r} has no op {name!r} "
        f"(known: {', '.join(REGISTRY.names())})")


class CoreView:
    """`obj.core` — the same ops, awaited.

    Not a mirrored API: there is nothing to import and nothing to construct.
    It exists so callers already inside an event loop reach the engine
    without the sync surface blocking their loop.
    """

    __slots__ = ("_obj",)

    def __init__(self, obj: Any) -> None:
        object.__setattr__(self, "_obj", obj)

    def __getattr__(self, name: str) -> Any:
        obj = self._obj
        spec = _spec_for(obj, name)

        async def call(*args: Any, **kwargs: Any) -> Any:
            _check_capability(obj, spec)
            result = spec.fn(obj, *args, **kwargs)
            if inspect.isawaitable(result):
                return await result
            return result

        call.__name__ = name
        return call() if spec.is_property else call

    def __dir__(self) -> list[str]:
        return sorted({s.name for klass in type(self._obj).__mro__
                       for s in REGISTRY.all() if s.owner == klass.__name__})

    def __repr__(self) -> str:
        return f"<core view of {type(self._obj).__name__}>"
