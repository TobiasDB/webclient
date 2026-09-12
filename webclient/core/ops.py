"""Op dispatch (PLAN §9): the surface objects (Reference/Document/Collection/
Field) are data; their *behaviour* lives here as registered ops, and the
executor runs a plan step by dispatching through this module rather than
calling a method on the value. Each op is wrapped by the same ``@policy``
envelope the methods used, so error/capability semantics are unchanged.

Migration is incremental and green: an op not yet moved off its class falls
back to the method on the value. Once every op is registered, the classes
carry no methods and the fallback is dead.
"""
from __future__ import annotations

from typing import Any, Callable

#: op-name -> callable(value, *args, **kwargs); @policy-wrapped like the methods
CALL_OPS: dict[str, Callable[..., Any]] = {}
#: computed accessors read as a bare attribute (no call): name -> fn(value)
PROP_OPS: dict[str, Callable[..., Any]] = {}


def op(name: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Register ``fn(self, ...)`` as the call-op ``name`` (``self`` = the value
    the executor is walking). Decorate the @policy-wrapped function."""
    def register(fn: Callable[..., Any]) -> Callable[..., Any]:
        CALL_OPS[name] = fn
        return fn
    return register


def prop(name: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Register ``fn(self)`` as the computed accessor ``name`` (a bare read)."""
    def register(fn: Callable[..., Any]) -> Callable[..., Any]:
        PROP_OPS[name] = fn
        return fn
    return register


def run_op(value: Any, name: str, args: list[Any], kwargs: dict[str, Any]) -> Any:
    """Dispatch a called op. Falls back to a method on ``value`` for any op
    not yet migrated into the registry."""
    fn = CALL_OPS.get(name)
    if fn is not None:
        return fn(value, *args, **kwargs)
    return getattr(value, name)(*args, **kwargs)


def read_prop(value: Any, name: str) -> Any:
    """Read a bare attribute: a registered computed accessor, else the value's
    own attribute (a plain data field, or a not-yet-migrated property)."""
    fn = PROP_OPS.get(name)
    if fn is not None:
        return fn(value)
    return getattr(value, name)


__all__ = ["CALL_OPS", "PROP_OPS", "op", "prop", "run_op", "read_prop"]
