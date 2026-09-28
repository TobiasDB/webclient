"""EventBacking: events routed onto the document."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, overload

from ...kernel.models import E
from ..web_core import Backing

if TYPE_CHECKING:
    from ...kernel.models import ActionEvent, Event
    from . import Document


class EventBacking(Backing):
    """Events routed onto the document. ``events`` is everything captured;
    ``events_of(cls)`` narrows by type (or by topic prefix ``str``);
    ``action_events`` is the interaction subset (empty until a live document)."""

    provides = frozenset({"events_of"})
    props = frozenset({"events", "action_events"})
    gate = "ok"

    def applies(self, core: "Document") -> bool:
        """Always in play -- every document carries an (possibly empty) event store."""
        return True

    def events(self, core: "Document") -> "list[Event]":
        """The document's whole captured-event store (the live, appendable list)."""
        return core._events  # the live store (appendable)

    @overload
    def events_of(self, core: "Document", event_type: type[E]) -> "list[E]":
        """Events of a given ``Event`` subclass."""
        ...
    @overload
    def events_of(self, core: "Document", event_type: str) -> "list[Event]":
        """Events whose topic matches a prefix string."""
        ...
    def events_of(self, core: "Document", event_type: Any) -> "list[Any]":
        """Events narrowed to a kind: by an ``Event`` subclass (isinstance) or by a topic
        prefix given as a string."""
        if isinstance(event_type, str):  # a topic prefix
            from ...kernel.models import topic_matches

            return [e for e in core._events if topic_matches(event_type, e.topic)]
        return [e for e in core._events if isinstance(e, event_type)]

    def action_events(self, core: "Document") -> "list[ActionEvent]":
        """The interaction subset of the events (clicks/writes/…); empty until a live document."""
        from ...kernel.models import ActionEvent

        return [e for e in core._events if isinstance(e, ActionEvent)]


__all__ = ["EventBacking"]
