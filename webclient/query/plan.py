"""The plan IR: the serializable form of a recorded expression.

An ``Expr`` (see ``webclient.expr``) records attribute access, calls, operators
and branches into a ``Plan`` -- a pydantic model, so a plan is the wire form for
the service/remote backends. The plan knows nothing about the cores; the
executor (``webclient.executor``) walks it against a live context.
"""

from __future__ import annotations

import json
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
    name: str = ""  # attribute / operator / function name
    args: list[Arg] = []
    kwargs: dict[str, Arg] = {}


#: the surface types a plan may be rooted at ("" = the evaluation context;
#: "WebClient" = the bound client, whose authoring verbs the executor dispatches)
ROOTS = frozenset({"", "WebClient", "Reference", "Document", "Collection", "Field"})
#: operators recordable as ``op`` steps
OPERATORS = frozenset({"eq", "ne", "lt", "le", "gt", "ge", "and", "or", "not"})
#: free functions recordable as ``fn`` steps
FUNCTIONS = frozenset({"is_empty", "is_ok"})

#: op name -> its Python symbol (so ``describe`` is a parseable expression, and
#: ``from_describe`` reads it back). ``not`` is the unary ``~`` prefix.
_OP_SYM = {
    "eq": "==", "ne": "!=", "lt": "<", "le": "<=", "gt": ">", "ge": ">=",
    "and": "&", "or": "|", "not": "~",
}
#: the token for the empty (evaluation-context) root in ``describe`` output.
_CTX = "_"


def _url_from_source(source: dict[str, Any]) -> str:
    """Reconstruct the seed URL from a ``reference(url)`` root's source spec, so
    ``describe`` can render (and ``from_describe`` re-read) it as ``reference("url")``.
    Pure string work -- the plan stays core-agnostic."""
    from urllib.parse import urlencode, urlunsplit

    host = source.get("hostname") or ""
    port = source.get("port")
    netloc = f"{host}:{port}" if port else host
    params = source.get("params") or {}
    query = urlencode(params, doseq=True) if params else ""
    url: str = urlunsplit(
        (source.get("scheme") or "https", netloc, source.get("path") or "",
         query, source.get("fragment") or "")
    )
    return url


class Plan(BaseModel):
    """A recorded chain: an optional ``root`` type name, an optional ``source``
    (the request spec a ``reference(url)`` root starts from), and its steps."""

    version: int = 1
    root: str = ""
    source: dict[str, Any] | None = None
    session_id: str | None = None
    steps: list[Step] = []

    def extend(self, step: Step) -> "Plan":
        return self.model_copy(update={"steps": [*self.steps, step]})

    def to_blob(self) -> str:
        """A portable, self-describing blob for the whole expression: compact JSON
        (only non-default fields). The inverse of :meth:`from_blob`. Plain JSON --
        no compression -- so rebuilding it is an ordinary parse (an LLM can author,
        pass around, rebuild, validate and pretty-print a plan as one JSON string)."""
        data = self.model_dump(exclude_defaults=True, exclude_none=True)
        return json.dumps(data, separators=(",", ":"), sort_keys=True)

    @classmethod
    def from_blob(cls, blob: str) -> "Plan":
        """Rebuild a plan from :meth:`to_blob`'s output (a JSON object string).
        Raises ``ValueError`` on malformed JSON / a non-object so the caller can
        report it; call ``validate_names`` after to enforce the safety boundary."""
        try:
            data = json.loads(blob)
        except ValueError as exc:
            raise ValueError(f"invalid plan blob: {exc}") from exc
        if not isinstance(data, dict):
            raise ValueError("plan blob did not decode to an object")
        return cls.model_validate(data)

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
        """A clean, canonical rendering of the chain as a Python-like expression --
        readable for logs/the demo, and **round-trippable**: ``from_describe`` parses
        this back into the same plan. Roots render as ``reference("url")`` (sourced)
        or the type name (``Document``/``Reference``/…), operators as their symbols
        (``==`` / ``&`` / ``~`` …), so the whole thing reads as the ``wq`` chain that
        recorded it."""
        if self.source is not None:
            out = f"reference({_url_from_source(self.source)!r})"
        else:
            out = self.root or _CTX
        for s in self.steps:
            args = ", ".join(
                [*map(_show, s.args), *(f"{k}={_show(v)}" for k, v in s.kwargs.items())]
            )
            if s.kind == "get":
                out = f"{out}.{s.name}"
            elif s.kind == "call":
                out = f"{out}({args})"
            elif s.kind == "op":
                out = f"~{out}" if s.name == "not" else f"({out} {_OP_SYM[s.name]} {args})"
            elif s.kind == "fn":
                out = f"{s.name}({out}{', ' + args if args else ''})"
            else:  # when
                out = f"when({args})"
        return out


def _show(arg: Arg) -> str:
    return arg.plan.describe() if arg.plan is not None else repr(arg.value)


Arg.model_rebuild()


__all__ = ["Arg", "Step", "Plan", "ROOTS", "OPERATORS", "FUNCTIONS"]
