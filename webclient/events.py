"""Core event taxonomy, EventBus and EventRegistry.

Topics are dotted strings matched by prefix: subscribing to "network" also
receives "network.xhr". Plugin events subclass one of these core events and
may introduce namespaced topics ("rrweb.dom.update").
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable, Literal, TypeVar
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, PrivateAttr

Topic = str


class Event(BaseModel):
    topic: Topic
    source: str = "core"  # name of the emitting plugin
    seq: int | None = None  # per-document counter, stamped by the bus
    ts: float | None = None  # stamped by the bus
    # correlation ids -- overwritten by Surface.emit (ISSUES #15)
    session_id: str | None = None
    document_id: str | None = None
    plan_id: str | None = None
    node_id: str | None = None  # stable node identity, stamped by the
    # capture plugin (enables LiveNode
    # event narrowing; ISSUES #9)


E = TypeVar("E", bound=Event)


# -- network ---------------------------------------------------------------- #


class NetworkEvent(Event):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    topic: Topic = "network"
    request: Any = None  # the ReferenceCore for this request
    status_code: int | None = None
    body: bytes | None = None


class NavigationEvent(NetworkEvent):
    topic: Topic = "network.navigation"


# -- dom -------------------------------------------------------------------- #


class DOMEvent(Event):
    topic: Topic = "dom"
    selector: str | None = None
    detail: dict[str, Any] = {}


class DOMUpdateEvent(DOMEvent):
    topic: Topic = "dom.update"
    kind: Literal["added", "removed", "attribute", "text"] = "added"


# -- interaction & console --------------------------------------------------- #


class ActionEvent(Event):
    topic: Topic = "action"
    action: str  # "click", "write", "scroll", ...
    args: dict[str, Any] = {}


class ConsoleEvent(Event):
    topic: Topic = "console"
    level: Literal["log", "info", "warning", "error"]
    text: str


class PlanEvent(Event):
    topic: Topic = "plan"
    phase: str = "started"  # started / row / done
    detail: dict[str, Any] = {}


CORE_EVENTS: tuple[type[Event], ...] = (
    NetworkEvent,
    NavigationEvent,
    DOMEvent,
    DOMUpdateEvent,
    ActionEvent,
    ConsoleEvent,
)


def _topic_matches(pattern: Topic, topic: Topic) -> bool:
    return not pattern or topic == pattern or topic.startswith(pattern + ".")


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
            if not _topic_matches(pattern, event.topic):
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
