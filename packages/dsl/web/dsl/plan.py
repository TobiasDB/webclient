"""The Plan: a recorded chain across surfaces, serialisable so it runs anywhere.

A Plan has three parts, one per surface it may cross:
  * ``url``     -- the root reference (what to load),
  * ``actions`` -- Reference-DSL steps that DRIVE the page (click / type / wait); they return
    Self in the surface, so a chain of them snapshots nothing until the join,
  * ``reads``   -- Document-DSL steps applied after the ``.doc()`` join (select / text / ...).

Because it is pure data, the same recorded chain runs locally (sync/async) or ships to a
server (API/remote dispatch). ``actions`` and ``reads`` being separate lists is the recorded
form of the surface boundary: everything before ``.doc()`` drives, everything after reads.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class Step(BaseModel):
    """One recorded call: ``op(*args)`` -- a Reference action or a Document read."""

    op: str
    args: list[Any] = []


class Plan(BaseModel):
    """A root URL, the Reference actions that drive it, and the Document reads after ``.doc()``."""

    url: str
    actions: list[Step] = []
    reads: list[Step] = []

    def to_blob(self) -> str:
        """Serialise to JSON -- what API/remote dispatch ships over the wire."""
        return self.model_dump_json()

    @classmethod
    def from_blob(cls, blob: str) -> "Plan":
        """Rebuild a Plan from :meth:`to_blob` output (the server side of remote dispatch)."""
        return cls.model_validate_json(blob)


__all__ = ["Plan", "Step"]
