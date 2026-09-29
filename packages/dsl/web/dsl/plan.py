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

from pydantic import BaseModel, JsonValue


class Step(BaseModel):
    """One recorded call: ``op(*args)`` -- a Reference action or a Document read. Args are plain
    JSON (:class:`~pydantic.JsonValue`), so a Plan serialises to a blob for remote dispatch."""

    op: str
    args: list[JsonValue] = []


class Plan(BaseModel):
    """A root URL, the Reference actions that drive it, and the Document reads after ``.doc()``.

    ``follow`` + ``doc_reads`` record a ``documents(column)`` join: after ``reads`` produce rows,
    each row's ``follow`` column is a URL to resolve, and ``doc_reads`` are applied to each resolved
    detail Document (results concatenated). ``follow=""`` means no such join (the common case)."""

    url: str
    actions: list[Step] = []
    reads: list[Step] = []
    follow: str = ""            # a row column holding a detail-page URL to resolve+extract per row
    doc_reads: list[Step] = []  # reads applied to each resolved detail Document

    def to_blob(self) -> str:
        """Serialise to JSON -- what API/remote dispatch ships over the wire."""
        return self.model_dump_json()

    @classmethod
    def from_blob(cls, blob: str) -> "Plan":
        """Rebuild a Plan from :meth:`to_blob` output (the server side of remote dispatch)."""
        return cls.model_validate_json(blob)


__all__ = ["Plan", "Step"]
