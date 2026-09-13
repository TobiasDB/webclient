"""The plan IR: the serializable form of a recorded expression.

An ``Expr`` (see ``webclient.expr``) records attribute access, calls, operators
and branches into a ``Plan`` -- a pydantic model, so a plan is the wire form for
the service/remote backends. The plan knows nothing about the cores; the
executor (``webclient.executor``) walks it against a live context.
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel


class Arg(BaseModel):
    """A call/operator argument: a plain ``value`` or a sub-expression
    ``plan`` (evaluated against the surrounding context at run time)."""

    value: Any = None
    plan: "Plan | None" = None


class Step(BaseModel):
    """One recorded operation in a chain."""

    kind: Literal["get", "call", "op", "fn", "when"]
    name: str = ""                       # attribute / operator / function name
    args: list[Arg] = []
    kwargs: dict[str, Arg] = {}


#: the surface types a plan may be rooted at ("" = the evaluation context)
ROOTS = frozenset({"", "Reference", "Document", "Collection", "Field"})
#: operators recordable as ``op`` steps
OPERATORS = frozenset({"eq", "ne", "lt", "le", "gt", "ge", "and", "or", "not"})
#: free functions recordable as ``fn`` steps
FUNCTIONS = frozenset({"is_empty", "is_ok"})


class Plan(BaseModel):
    """A recorded chain: an optional ``root`` type name, an optional ``source``
    (the request spec a ``reference(url)`` root starts from), and its steps."""

    version: int = 1
    root: str = ""
    source: dict[str, Any] | None = None
    steps: list[Step] = []

    def extend(self, step: Step) -> "Plan":
        return self.model_copy(update={"steps": [*self.steps, step]})

    def validate_names(self) -> "Plan":
        """Reject a plan that could not have been legitimately recorded: an
        unknown root, a private (``_``) name, an unknown operator/function. Any
        other public name is allowed (no whitelist -- ``_``-refusal is the
        safety boundary, since the ``__subclasses__`` escape needs a dunder)."""
        if self.root not in ROOTS:
            raise ValueError(f"unknown plan root {self.root!r}")
        for step in self.steps:
            if step.name.startswith("_"):
                raise ValueError(f"private name {step.name!r} in plan")
            if step.kind == "op" and step.name not in OPERATORS:
                raise ValueError(f"unknown operator {step.name!r} in plan")
            if step.kind == "fn" and step.name not in FUNCTIONS:
                raise ValueError(f"unknown function {step.name!r} in plan")
            for arg in (*step.args, *step.kwargs.values()):
                if arg.plan is not None:
                    arg.plan.validate_names()
        return self

    def describe(self) -> str:
        """A readable rendering of the chain (for logs / the demo)."""
        out = f"{self.root or 'reference'}({self.source.get('hostname', '')})" \
            if self.source else (self.root or "·")
        for s in self.steps:
            args = ", ".join([*map(_show, s.args),
                              *(f"{k}={_show(v)}" for k, v in s.kwargs.items())])
            out = {"get": f"{out}.{s.name}",
                   "call": f"{out}({args})",
                   "op": f"({out} {s.name} {args})",
                   "fn": f"{s.name}({out}{', ' + args if args else ''})",
                   "when": f"when({args})"}[s.kind]
        return out


def _show(arg: Arg) -> str:
    return arg.plan.describe() if arg.plan is not None else repr(arg.value)


Arg.model_rebuild()


__all__ = ["Arg", "Step", "Plan", "ROOTS", "OPERATORS", "FUNCTIONS"]
