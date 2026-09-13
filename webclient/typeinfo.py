"""Type classification for the surface generator.

Given a resolved annotation, decide which surface category it maps into
(``classify``) and pull it apart (``element_type`` / ``unwrap_union`` /
``field_type``). ``scripts/gen_stubs.py`` reads the signatures (it owns the
``get_type_hints`` / ``inspect`` calls, since it needs a custom namespace) and
leans on this module for the classification.
"""

from __future__ import annotations

import types
import typing
from collections.abc import Iterable as _Iterable
from collections.abc import Mapping as _Mapping
from typing import Any


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
    if (
        origin is not None
        and isinstance(origin, type)
        and issubclass(origin, _Iterable)
        and not issubclass(origin, _Mapping)
        and origin not in (str, bytes)
    ):
        return "iterable"  # list/tuple/set/Sequence -> Collection
    return "scalar"  # dict/Mapping/scalars stay data


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


__all__ = [
    "field_type",
    "classify",
    "element_type",
    "unwrap_union",
]
