"""Event backing (PLAN §9): queries over a document's captured event store.

Consolidates the event views that used to be methods/properties on Document
(``events_of`` / ``action_events`` / ``xhr_requests`` / ``dom_mutations`` /
``console`` / ``subscribe``). Gate ``ok`` and always in the backing list, so
any resolved document has them; element-narrowing uses the live locator's
captured identity when present.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Sequence

from .base import Backing
from ...events import (ActionEvent, ConsoleEvent, DOMUpdateEvent, Event, Topic,
                       XHREvent)

if TYPE_CHECKING:
    from ..document import DocumentCore


class EventBacking(Backing):
    provides = frozenset({"events_of", "action_events", "xhr_requests",
                          "dom_mutations", "console", "subscribe"})
    gate = "ok"

    def events_of(self, core: "DocumentCore",
                  event: "type[Event] | Topic") -> Sequence[Event]:
        doc = core.doc
        if isinstance(event, str):
            matches = [e for e in doc.events
                       if e.topic == event or e.topic.startswith(event + ".")]
        else:
            matches = [e for e in doc.events if isinstance(e, event)]
        path = core.identity_path() if core.locator is not None else None
        if path is None:
            return matches
        return [e for e in matches if e.node_id is not None
                and (e.node_id == path or e.node_id.startswith(path + "/"))]

    def action_events(self, core: "DocumentCore") -> Sequence[ActionEvent]:
        return self.events_of(core, ActionEvent)  # type: ignore[return-value]

    def xhr_requests(self, core: "DocumentCore") -> Sequence[XHREvent]:
        return self.events_of(core, XHREvent)  # type: ignore[return-value]

    def dom_mutations(self, core: "DocumentCore") -> Sequence[DOMUpdateEvent]:
        return self.events_of(core, DOMUpdateEvent)  # type: ignore[return-value]

    def console(self, core: "DocumentCore") -> Sequence[ConsoleEvent]:
        return self.events_of(core, ConsoleEvent)  # type: ignore[return-value]

    def subscribe(self, core: "DocumentCore", topic: Topic, handler: Any) -> Any:
        doc = core.doc
        return doc._client.bus.subscribe(topic, handler, document_id=doc.id)
