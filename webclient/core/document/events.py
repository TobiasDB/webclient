"""EventBacking: events routed onto the document."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..web_core import Backing

if TYPE_CHECKING:
    from . import DocumentCore


class EventBacking(Backing):
    """Events routed onto the document. ``events`` is everything captured;
    ``events_of(cls)`` narrows by type; ``action_events`` is the interaction
    subset (empty until a live/browser document)."""

    provides = frozenset({"events_of"})
    props = frozenset({"events", "action_events"})
    gate = "ok"

    def applies(self, core: "DocumentCore") -> bool:
        return True

    def events(self, core: "DocumentCore") -> list[Any]:
        return core._events  # the live store (appendable)

    def events_of(self, core: "DocumentCore", event_type: Any) -> list[Any]:
        if isinstance(event_type, str):  # a topic prefix
            from ...events import _topic_matches

            return [e for e in core._events if _topic_matches(event_type, e.topic)]
        return [e for e in core._events if isinstance(e, event_type)]

    def action_events(self, core: "DocumentCore") -> list[Any]:
        from ...events import ActionEvent

        return [e for e in core._events if isinstance(e, ActionEvent)]


__all__ = ["EventBacking"]
