"""The lazy query layer.

One recorder (:mod:`~webclient.query.expr` -- the ``Expr`` engine), one
serialisable IR (:mod:`~webclient.query.plan` -- ``Plan``), and one async
evaluator (:mod:`~webclient.query.executor`). Grouped under this package so the
query machinery is separable from the cores/surfaces it drives. The lazy
authoring layer (``wq``/``doc``/``ref``/``many`` + the free builders) lives with
the lazy surfaces (:mod:`webclient.surfaces.lazy`), re-exported from
:mod:`webclient`.
"""

from __future__ import annotations

from .executor import (
    aevaluate,
    astream,
    evaluate,
    fan_out,
    fan_out_stream,
    truthy,
)
from .expr import Expr, from_explain, from_plan, lazy, lazy_root, to_arg
from .plan import Arg, Plan, Step

__all__ = [
    # expr recorder engine
    "Expr",
    "lazy",
    "lazy_root",
    "from_plan",
    "from_explain",
    "to_arg",
    # plan IR
    "Plan",
    "Arg",
    "Step",
    # executor
    "aevaluate",
    "astream",
    "evaluate",
    "fan_out",
    "fan_out_stream",
    "truthy",
]
