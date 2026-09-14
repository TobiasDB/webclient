"""EventBus and EventRegistry -- the runtime machinery over the event taxonomy.

The event *models* (``Event`` and its subclasses) live in
:mod:`webclient.models` (the shared, dependency-light data models); this module
holds the pub/sub bus and the topic->class registry, and re-exports the event
classes so ``from webclient.events import NetworkEvent`` keeps working.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable
from uuid import uuid4

from pydantic import BaseModel, PrivateAttr

from .models import (
    CORE_EVENTS,
    ActionEvent,
    ConsoleEvent,
    DOMEvent,
    DOMUpdateEvent,
    E,
    Event,
    NavigationEvent,
    NetworkEvent,
    PlanEvent,
    Topic,
    topic_matches,
)

#: back-compat alias (the private name callers imported before the split).
_topic_matches = topic_matches


# --------------------------------------------------------------------------- #
# EventBus
# --------------------------------------------------------------------------- #


class Subscription(BaseModel):
    id: str
    topic: Topic

    _bus: Any = PrivateAttr(default=None)

    def cancel(self) -> None:
        if self._bus is not None:
            self._bus._remove(self.id)
            self._bus = None


class EventBus(BaseModel):
    """Shared pub/sub. Synchronous dispatch on the publisher's thread;
    handlers must not block and must not call sync facade methods.

    The bus stamps ``seq`` (monotonic per document id; a shared stream for
    events without one) and ``ts`` on every publish (ISSUES #15).
    """

    _lock: Any = PrivateAttr(default_factory=threading.RLock)
    _subs: dict[str, tuple[Topic, dict[str, str | None], Callable[[Event], None]]] = (
        PrivateAttr(default_factory=dict)
    )
    _seq: dict[str | None, int] = PrivateAttr(default_factory=dict)

    def publish(self, event: Event) -> None:
        with self._lock:
            key = event.document_id
            self._seq[key] = self._seq.get(key, 0) + 1
            event.seq = self._seq[key]
            event.ts = time.time()
            subs = list(self._subs.values())
        for pattern, filters, handler in subs:
            if not topic_matches(pattern, event.topic):
                continue
            if any(
                getattr(event, field) != value
                for field, value in filters.items()
                if value is not None
            ):
                continue
            handler(event)

    def subscribe(
        self,
        topic: Topic,
        handler: Callable[[Event], None],
        *,
        session_id: str | None = None,
        document_id: str | None = None,
        plan_id: str | None = None,
    ) -> Subscription:
        """Handler fires for events whose topic matches ``topic`` by dotted
        prefix ("" matches everything) and every given correlation filter."""
        sub_id = uuid4().hex
        filters = {
            "session_id": session_id,
            "document_id": document_id,
            "plan_id": plan_id,
        }
        with self._lock:
            self._subs[sub_id] = (topic, filters, handler)
        sub = Subscription(id=sub_id, topic=topic)
        sub._bus = self
        return sub

    def _remove(self, sub_id: str) -> None:
        with self._lock:
            self._subs.pop(sub_id, None)


# --------------------------------------------------------------------------- #
# EventRegistry
# --------------------------------------------------------------------------- #


class EventRegistry(BaseModel):
    """Topic -> event class, for typed round-tripping over the wire and for
    resolving event types referenced in plans. Core events pre-registered;
    unknown topics resolve to the nearest registered ancestor (trailing
    segments dropped first, then the leading namespace), falling back to
    Event."""

    _by_topic: dict[str, type[Event]] = PrivateAttr(default_factory=dict)

    def model_post_init(self, __context: Any) -> None:
        for cls in CORE_EVENTS:
            self.register(cls)

    def register(self, cls: type[Event]) -> None:
        topic = cls.model_fields["topic"].default
        if isinstance(topic, str) and topic:
            self._by_topic[topic] = cls

    def resolve(self, topic: Topic) -> type[Event]:
        parts = topic.split(".")
        for end in range(len(parts), 0, -1):
            found = self._by_topic.get(".".join(parts[:end]))
            if found is not None:
                return found
        if len(parts) > 1:
            return self.resolve(".".join(parts[1:]))
        return Event


__all__ = [
    # taxonomy (re-exported from webclient.models)
    "Topic",
    "Event",
    "E",
    "NetworkEvent",
    "NavigationEvent",
    "DOMEvent",
    "DOMUpdateEvent",
    "ActionEvent",
    "ConsoleEvent",
    "PlanEvent",
    "CORE_EVENTS",
    "topic_matches",
    # machinery (defined here)
    "EventBus",
    "EventRegistry",
    "Subscription",
]
