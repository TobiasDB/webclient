"""web.resolve's data models -- the event type its policies report on the bus."""

from __future__ import annotations

from pydantic import BaseModel


class ResolveEvent(BaseModel):
    """A resolve-policy step: a retry attempt, a tier climb, or a fetched page. ``phase`` names
    which (``retry`` / ``escalate`` / ``page``); ``url`` and ``detail`` give context."""

    topic: str = "resolve"
    phase: str = ""
    url: str = ""
    detail: dict[str, object] = {}


__all__ = ["ResolveEvent"]
