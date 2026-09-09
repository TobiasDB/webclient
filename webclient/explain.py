"""`plan.explain()` — the plan as a tree, with what each step costs."""
from __future__ import annotations

from typing import Any

from .ops import REGISTRY
from .plan import (BinOpStep, CallStep, ExplodeStep, FilterStep, LimitStep,
                   LiteralStep, MapStep, OtherwiseStep, Plan, ThenStep)


def explain_plan(plan: Plan) -> str:
    lines = [f"source: {plan.source.describe()}"]
    _steps(plan.steps, lines, 0)
    return "\n".join(lines)


def _pad(depth: int) -> str:
    return "  " * depth


def _cost(op_name: str) -> str:
    spec = REGISTRY.any_named(op_name)
    if spec is None:
        return ""
    if spec.resource:
        return f"  [{spec.resource} lease]"
    if spec.pure:
        return "  [pure]"
    return ""


def _steps(steps: Any, lines: list[str], depth: int) -> None:
    for step in steps:
        if isinstance(step, CallStep):
            args = [_arg(a) for a in step.args]
            args += [f"{k}={_arg(v)}" for k, v in step.kwargs.items()
                     if v.value not in (False, None)]
            lines.append(f"{_pad(depth)}{step.op}({', '.join(args)})"
                         f"{_cost(step.op)}")
        elif isinstance(step, BinOpStep):
            lines.append(f"{_pad(depth)}{step.operator} {_arg(step.right)}")
        elif isinstance(step, LiteralStep):
            lines.append(f"{_pad(depth)}literal {step.value!r}")
        elif isinstance(step, (ThenStep, MapStep)):
            label = "then" if isinstance(step, ThenStep) else "map"
            lines.append(f"{_pad(depth)}{label}")
            for projection in step.fields:
                lines.append(f"{_pad(depth + 1)}{projection.name} =")
                _steps(projection.plan.steps, lines, depth + 2)
        elif isinstance(step, FilterStep):
            lines.append(f"{_pad(depth)}filter")
            _steps(step.predicate.steps, lines, depth + 1)
        elif isinstance(step, OtherwiseStep):
            if step.sentinel:
                lines.append(f"{_pad(depth)}otherwise -> {step.sentinel.upper()}")
            else:
                names = ", ".join(p.name for p in step.recovery or [])
                lines.append(
                    f"{_pad(depth)}otherwise -> record{{ok, {names}}}")
        elif isinstance(step, ExplodeStep):
            lines.append(f"{_pad(depth)}explode({step.path})")
        elif isinstance(step, LimitStep):
            lines.append(f"{_pad(depth)}limit({step.count})")
        else:                                        # pragma: no cover
            lines.append(f"{_pad(depth)}<unknown {type(step).__name__}>")


def _arg(arg: Any) -> str:
    if arg is None:
        return ""
    if arg.kind == "literal":
        return repr(arg.value)
    if arg.kind == "field":
        return f"field({arg.name!r})"
    return "<plan>"
