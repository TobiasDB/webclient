from typing import TYPE_CHECKING

from . import expr as _expr
from .expr import (Arg, Plan, Step, field, filter, from_plan, is_empty, is_ok,
                   lazy, when)

if TYPE_CHECKING:                       # typed for checkers; live at runtime
    from .expr import doc, many, ref

__all__ = ["Plan", "Step", "Arg", "doc", "field", "filter", "from_plan",
           "is_empty", "is_ok", "lazy", "many", "ref", "when"]


def __getattr__(name: str):
    # doc / many / ref are installed after models loads (they need the model
    # classes); read them live so import order cannot bind a stale None.
    if name in ("doc", "many", "ref"):
        return getattr(_expr, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
