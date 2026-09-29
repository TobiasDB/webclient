"""The plan IR: the serialisable form of a recorded expression.

An :class:`~web.dsl.expr.Expr` records attribute access, calls, operators and branches into a
``Plan`` -- a pydantic model, so a plan is the wire form (``to_blob``) the service/remote modes
ship. The plan knows nothing about the cores; the executor (:mod:`web.dsl.run`) walks it against a
live context. Ported from the monolith's query IR so the two versions speak ONE plan language.
"""

from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, JsonValue


class Arg(BaseModel):
    """A call/operator argument: a plain ``value`` or a sub-expression ``plan`` (evaluated against
    the surrounding element/context at run time -- this is how ``extract(title=<sub-expr>)`` works).
    """

    value: JsonValue = None
    plan: "Plan | None" = None


class Step(BaseModel):
    """One recorded operation in a chain -- an attribute ``get``, a ``call``, a binary/unary ``op``,
    a free ``fn`` (``is_ok``/``is_empty``), or a ``when`` branch."""

    kind: Literal["get", "call", "op", "fn", "when"]
    name: str = ""  # attribute / operator / function name
    args: list[Arg] = []
    kwargs: dict[str, Arg] = {}


#: the surface types a plan may be rooted at ("" = the evaluation context, e.g. the element a
#: sub-expr runs against; the others are the lazy surfaces the recorder was cast to).
ROOTS = frozenset({"", "Reference", "Document", "Collection", "Field"})
#: operators recordable as ``op`` steps.
OPERATORS = frozenset({"eq", "ne", "lt", "le", "gt", "ge", "and", "or", "not"})
#: free functions recordable as ``fn`` steps.
FUNCTIONS = frozenset({"is_empty", "is_ok"})

#: op name -> its Python symbol, so ``describe`` renders as the ``wq`` chain that recorded it.
_OP_SYM = {
    "eq": "==",
    "ne": "!=",
    "lt": "<",
    "le": "<=",
    "gt": ">",
    "ge": ">=",
    "and": "&",
    "or": "|",
    "not": "~",
}
#: the token for the empty (evaluation-context) root in ``describe`` output.
_CTX = "_"


class Plan(BaseModel):
    """A recorded chain: an optional ``root`` type name, an optional ``source`` (the request URL a
    ``reference(url)`` root starts from), and its ``steps``. Immutable -- recording copies.
    """

    version: int = 1
    root: str = ""
    source: str | None = None
    steps: list[Step] = []

    def extend(self, step: Step) -> "Plan":
        """A copy with ``step`` appended -- recording never mutates a shared plan."""
        return self.model_copy(update={"steps": [*self.steps, step]})

    def to_blob(self) -> str:
        """A portable, self-describing blob: compact JSON of the non-default fields (plain JSON, no
        compression -- an LLM can author, pass around, rebuild and validate a plan as one string).
        """
        return json.dumps(
            self.model_dump(exclude_defaults=True, exclude_none=True),
            separators=(",", ":"),
            sort_keys=True,
        )

    @classmethod
    def from_blob(cls, blob: str) -> "Plan":
        """Rebuild a plan from :meth:`to_blob` output. Raises ``ValueError`` on malformed JSON; call
        :meth:`validate_names` after to enforce the safety boundary."""
        try:
            data = json.loads(blob)
        except ValueError as exc:
            raise ValueError(f"invalid plan blob: {exc}") from exc
        if not isinstance(data, dict):
            raise ValueError("plan blob did not decode to an object")
        return cls.model_validate(data)

    def validate_names(self) -> "Plan":
        """Reject a plan that could not have been legitimately recorded: an unknown root, a private
        (``_``) name, an unknown operator/function. The ``_``-refusal is the one safety boundary
        (any other public name is allowed) -- the wire guard for the service/remote modes.
        """
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
        """A canonical, readable rendering of the chain as a Python-like expression -- for logs and
        the demo. Roots render as ``reference("url")`` (sourced) or the type name; operators as
        their symbols, so the whole thing reads as the ``wq`` chain that recorded it."""
        out = (
            f"reference({self.source!r})"
            if self.source is not None
            else (self.root or _CTX)
        )
        for s in self.steps:
            args = ", ".join(
                [*map(_show, s.args), *(f"{k}={_show(v)}" for k, v in s.kwargs.items())]
            )
            if s.kind == "get":
                out = f"{out}.{s.name}"
            elif s.kind == "call":
                out = f"{out}({args})"
            elif s.kind == "op":
                out = (
                    f"~{out}"
                    if s.name == "not"
                    else f"({out} {_OP_SYM[s.name]} {args})"
                )
            elif s.kind == "fn":
                out = f"{s.name}({out}{', ' + args if args else ''})"
            else:  # when
                out = f"when({args})"
        return out


def _show(arg: Arg) -> str:
    """Render one plan arg for :meth:`Plan.describe`: a sub-plan as its own describe form, a literal
    as its repr."""
    return arg.plan.describe() if arg.plan is not None else repr(arg.value)


Arg.model_rebuild()


__all__ = ["Arg", "Step", "Plan", "ROOTS", "OPERATORS", "FUNCTIONS"]
