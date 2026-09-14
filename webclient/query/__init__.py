"""The lazy query layer.

One recorder (:mod:`~webclient.query.expr` -- ``Expr`` and the ``wq`` authoring
namespace), one serialisable IR (:mod:`~webclient.query.plan` -- ``Plan``), and
one async evaluator (:mod:`~webclient.query.executor`). Grouped under this
package so the query machinery is separable from the cores/surfaces it drives.
The public names are re-exported here (and again from :mod:`webclient`).
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
from .expr import (
    Expr,
    WebQuery,
    doc,
    field,
    filter,
    from_plan,
    is_empty,
    is_ok,
    lazy,
    many,
    ref,
    reference,
    to_arg,
    when,
    wq,
)
from .plan import Arg, Plan, Step

__all__ = [
    # expr / authoring
    "Expr",
    "WebQuery",
    "wq",
    "doc",
    "ref",
    "many",
    "reference",
    "field",
    "filter",
    "when",
    "is_ok",
    "is_empty",
    "lazy",
    "from_plan",
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
