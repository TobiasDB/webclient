"""EventBacking: events routed onto the document."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, overload

from ...models import E
from ..web_core import Backing

if TYPE_CHECKING:
    from ...models import ActionEvent, Event
    from . import DocumentCore


class EventBacking(Backing):
    """Events routed onto the document. ``events`` is everything captured;
    ``events_of(cls)`` narrows by type (or by topic prefix ``str``);
    ``action_events`` is the interaction subset (empty until a live document)."""

    provides = frozenset({"events_of"})
    props = frozenset({"events", "action_events"})
    gate = "ok"

    def applies(self, core: "DocumentCore") -> bool:
        return True

    def events(self, core: "DocumentCore") -> "list[Event]":
        return core._events  # the live store (appendable)

    @overload
    def events_of(self, core: "DocumentCore", event_type: type[E]) -> "list[E]": ...
    @overload
    def events_of(self, core: "DocumentCore", event_type: str) -> "list[Event]": ...
    def events_of(self, core: "DocumentCore", event_type: Any) -> "list[Any]":
        if isinstance(event_type, str):  # a topic prefix
            from ...models import topic_matches

            return [e for e in core._events if topic_matches(event_type, e.topic)]
        return [e for e in core._events if isinstance(e, event_type)]

    def action_events(self, core: "DocumentCore") -> "list[ActionEvent]":
        from ...models import ActionEvent

        return [e for e in core._events if isinstance(e, ActionEvent)]


__all__ = ["EventBacking"]
