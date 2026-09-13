"""The eager surface: thin runtime wrappers over a Core.

A ``Surface`` holds a core and turns attribute access into behaviour: a Core
data field reads through; a property op dispatches; a call op returns a
dispatcher. Every Core-typed result is auto-wrapped back into its surface, so
chaining stays on the surface. The generated stub classes (``webclient.gen``)
supply the static types; this is the one runtime behind all of them.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable, TypeVar

from .core.web_core import WebCore

#: core class -> its eager Surface class (filled by ``@surface``)
_REGISTRY: dict[type, type] = {}


def wrap(value: Any, *, client: Any = None) -> Any:
    """Core -> its Surface; a list of cores -> a ``Collection`` of surfaces;
    any other list/tuple -> the same with items wrapped; else as-is. A core's
    surface is cached on it, so wrapping the same core twice yields the same
    surface object (identity: ``wc.document(name) is shop``)."""
    if isinstance(value, WebCore):
        cached = getattr(value, "_surface", None)
        if cached is not None:
            return cached
        cls = _REGISTRY.get(type(value))
        if cls is None:
            return value
        surf = cls(value)
        try:
            value._surface = surf  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass
        return surf
    if isinstance(value, (list, tuple)):
        if not any(isinstance(v, WebCore) for v in value):
            return value  # nothing to wrap; preserve identity
        from .collection import Collection

        owner = client or getattr(value[0], "_client", None)
        root = getattr(value[0], "root", "") or getattr(value[0], "name", "")
        return Collection([wrap(v) for v in value], client=owner, root=root)
    return value


class Surface:
    """Runtime eager object: dispatches ops on ``_core``, wraps Core results."""

    __slots__ = ("_core",)

    def __init__(self, core: WebCore) -> None:
        object.__setattr__(self, "_core", core)

    def __setattr__(self, name: str, value: Any) -> None:
        # Binding a client onto a surface (doc._client = wc) routes to the core
        # (accepting a client surface or a core); everything else is normal.
        if name == "_client":
            core = object.__getattribute__(self, "_core")
            core._client = getattr(value, "_core", value)
        else:
            object.__setattr__(self, name, value)

    if not TYPE_CHECKING:  # hidden from type checkers:

        def __getattr__(self, name: str) -> Any:  # the typed surface is the
            if name.startswith("_"):  # generated stub blocks
                raise AttributeError(name)
            core = object.__getattribute__(self, "_core")
            cls = type(core)
            if name in cls.model_fields:  # a data field
                return getattr(core, name)
            if name in cls.prop_ops():  # a property op -> dispatch now
                return wrap(core.dispatch(name))
            if name in cls.ops():  # a call op -> a dispatcher

                def call(*args: Any, **kwargs: Any) -> Any:
                    return wrap(core.dispatch(name, *args, **kwargs))

                return call
            if isinstance(getattr(cls, name, None), property):  # a core property
                return wrap(getattr(core, name))
            raise AttributeError(name)

    def __repr__(self) -> str:
        return f"{type(self).__name__}({object.__getattribute__(self, '_core')!r})"


_C = TypeVar("_C", bound=type)


def surface(core_cls: type) -> "Callable[[_C], _C]":
    """Register the decorated Surface subclass as ``core_cls``'s eager wrapper
    (an identity decorator -- the class type is preserved)."""

    def register(cls: _C) -> _C:
        _REGISTRY[core_cls] = cls
        return cls

    return register


__all__ = ["Surface", "wrap", "surface"]
