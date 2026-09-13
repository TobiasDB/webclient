"""Return-type resolution for ``Expr`` validation + surface generation.

Given a Core's data fields and its backings' typed ops, work out each member's
result type and which surface category it maps into. This is the one place
that reads signatures (stdlib ``inspect`` / ``typing.get_type_hints``); both
``Expr`` validation and the surface generator (``gen``) use it.

(Grows out of the extraction in the old ``scripts/gen_stubs.py``.)
"""
from __future__ import annotations

import inspect
import types
import typing
from collections.abc import Iterable as _Iterable
from collections.abc import Mapping as _Mapping
from typing import Any


def return_type(fn: Any) -> Any:
    """The resolved return annotation of ``fn`` (``Any`` if none/unresolvable).
    Overloads: ``typing.get_overloads`` would refine by call args -- TODO."""
    try:
        return typing.get_type_hints(fn).get("return", Any)
    except Exception:
        return getattr(fn, "__annotations__", {}).get("return", Any)


def op_params(fn: Any) -> list[inspect.Parameter]:
    """The op's parameters, dropping the ``self``/``core`` receiver pair."""
    sig = inspect.signature(fn)
    return [p for n, p in sig.parameters.items() if n not in ("self", "core")]


def field_type(core_cls: type, name: str) -> Any:
    """A Core data field's type (pydantic ``model_fields`` -- plain getattr
    would miss these)."""
    field = getattr(core_cls, "model_fields", {}).get(name)
    return field.annotation if field is not None else Any


def classify(tp: Any, cores: tuple[type, ...]) -> str:
    """-> 'core' (a WebCore subtype -> its surface class), 'iterable'
    (list/Sequence -> Collection[T]/Iterable[T]) or 'scalar' (-> Field[T]/T)."""
    if isinstance(tp, type) and issubclass(tp, cores):
        return "core"
    origin = typing.get_origin(tp)
    if origin is not None and isinstance(origin, type) \
            and issubclass(origin, _Iterable) \
            and not issubclass(origin, _Mapping) \
            and origin not in (str, bytes):
        return "iterable"                  # list/tuple/set/Sequence -> Collection
    return "scalar"                        # dict/Mapping/scalars stay data



def element_type(tp: Any) -> Any:
    """The element type of a list/Sequence annotation (``Any`` if unparameterised)."""
    args = typing.get_args(tp)
    return args[0] if args else Any


def unwrap_union(tp: Any) -> Any:
    """A union -> its non-None member if single, else ``Any`` (nearest common
    base is a TODO); a plain type passes through."""
    if typing.get_origin(tp) in (typing.Union, types.UnionType):  # X|Y and Union[X, Y]
        parts = [a for a in typing.get_args(tp) if a is not type(None)]
        return parts[0] if len(parts) == 1 else Any
    return tp


__all__ = ["return_type", "op_params", "field_type", "classify",
           "element_type", "unwrap_union"]
