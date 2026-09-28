"""The Plan: a recorded chain of method calls, serialisable so it can run anywhere.

A Plan is *a root + a list of steps*. The root says how to obtain the first object (here: a URL
to resolve into a Document); each :class:`Step` is a method name plus its (JSON-serialisable)
arguments. Because a Plan is pure data, the same recorded chain runs locally (sync/async) or is
shipped to a server and run there (API/remote dispatch) -- that is the whole point of recording
instead of calling.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class Step(BaseModel):
    """One recorded method call: ``op(*args, **kwargs)`` against the running object."""

    op: str
    args: list[Any] = []
    kwargs: dict[str, Any] = {}


class Plan(BaseModel):
    """A root URL to resolve, then a chain of method calls to apply to the result."""

    url: str
    steps: list[Step] = []

    def then(self, op: str, args: tuple[Any, ...], kwargs: dict[str, Any]) -> "Plan":
        """A new Plan with one more step appended (Plans are immutable -- recording forks)."""
        return Plan(url=self.url, steps=[*self.steps, Step(op=op, args=list(args), kwargs=kwargs)])

    def to_blob(self) -> str:
        """Serialise to JSON -- what API/remote dispatch ships over the wire."""
        return self.model_dump_json()

    @classmethod
    def from_blob(cls, blob: str) -> "Plan":
        """Rebuild a Plan from :meth:`to_blob` output (the server side of remote dispatch)."""
        return cls.model_validate_json(blob)


__all__ = ["Plan", "Step"]
