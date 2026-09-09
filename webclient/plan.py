"""The plan IR.

A typed, versioned, serialisable description of an expression chain. Both
evaluation modes produce and consume exactly this: eager calls build a plan
and evaluate it immediately, lazy calls build the same plan and hand it back.
There is no second representation anywhere, which is what keeps recording and
evaluation from drifting.

Everything is a discriminated union so a malformed plan fails validation
rather than reaching the evaluator as an untyped dict.
"""
from __future__ import annotations

from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, Field as PydanticField

Sentinel = Literal["raise", "drop", "null"]


# --------------------------------------------------------------------------- #
# Sources
# --------------------------------------------------------------------------- #

class Source(BaseModel):
    """Where a plan starts. `context` needs a runtime object passed to
    collect(); the other two carry their own origin."""

    kind: Literal["context", "reference", "document"] = "context"
    url: str | None = None
    document_id: str | None = None
    # what the context is expected to be, for record-time validation
    root: str = "Document"

    def describe(self) -> str:
        if self.kind == "reference":
            return f"reference {self.url}"
        if self.kind == "document":
            return f"document {self.document_id}"
        return f"context ({self.root})"


# --------------------------------------------------------------------------- #
# Arguments
# --------------------------------------------------------------------------- #

class Arg(BaseModel):
    """An argument to a recorded call: a literal, a reference to a field
    already extracted in this context, or a nested plan."""

    kind: Literal["literal", "field", "plan"] = "literal"
    value: Any = None
    name: str | None = None
    plan: "Plan | None" = None


# --------------------------------------------------------------------------- #
# Steps
# --------------------------------------------------------------------------- #

class Projection(BaseModel):
    """One named column of a `then` / `map` / recovery block."""

    name: str
    plan: "Plan"


class CallStep(BaseModel):
    kind: Literal["call"] = "call"
    op: str
    args: list[Arg] = []
    kwargs: dict[str, Arg] = {}


class LiteralStep(BaseModel):
    kind: Literal["literal"] = "literal"
    value: Any = None


class BinOpStep(BaseModel):
    kind: Literal["binop"] = "binop"
    operator: Literal["eq", "ne", "lt", "le", "gt", "ge", "and", "or", "not",
                      "contains"]
    right: Arg | None = None


class ThenStep(BaseModel):
    """One context in, one record out."""

    kind: Literal["then"] = "then"
    fields: list[Projection] = []


class MapStep(BaseModel):
    """`then` lifted over a collection: many contexts in, many records out."""

    kind: Literal["map"] = "map"
    fields: list[Projection] = []


class FilterStep(BaseModel):
    kind: Literal["filter"] = "filter"
    predicate: "Plan"


class OtherwiseStep(BaseModel):
    """Recovery for everything recorded before it in this plan: either a
    sentinel policy or a projection evaluated against the failure."""

    kind: Literal["otherwise"] = "otherwise"
    sentinel: Sentinel | None = None
    recovery: list[Projection] | None = None


class ExplodeStep(BaseModel):
    kind: Literal["explode"] = "explode"
    path: str


class LimitStep(BaseModel):
    kind: Literal["limit"] = "limit"
    count: int


Step = Annotated[
    Union[CallStep, LiteralStep, BinOpStep, ThenStep, MapStep,
          FilterStep, OtherwiseStep, ExplodeStep, LimitStep],
    PydanticField(discriminator="kind"),
]


class Plan(BaseModel):
    """A source plus an ordered chain of steps."""

    version: int = 1
    source: Source = Source()
    steps: list[Step] = []

    def extend(self, step: Step) -> "Plan":
        return Plan(version=self.version, source=self.source,
                    steps=[*self.steps, step])

    @property
    def is_rooted(self) -> bool:
        return self.source.kind != "context"


Arg.model_rebuild()
Projection.model_rebuild()
FilterStep.model_rebuild()
Plan.model_rebuild()
