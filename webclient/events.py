"""EventBus and EventRegistry -- the runtime machinery over the event taxonomy.

The event *models* (``Event`` and its subclasses) live in
:mod:`webclient.models` (the shared, dependency-light data models); this module
holds the pub/sub bus and the topic->class registry, and re-exports the event
classes so ``from webclient.events import NetworkEvent`` keeps working.
"""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from collections import OrderedDict, deque
from typing import Any, Callable, Iterator
from uuid import uuid4

from pydantic import BaseModel, PrivateAttr

from .models import (
    CORE_EVENTS,
    ActionEvent,
    ConsoleEvent,
    DOMEvent,
    DOMUpdateEvent,
    E,
    ErrorEvent,
    Event,
    LoopEvent,
    NavigationEvent,
    NetworkEvent,
    PipelineEvent,
    PlanEvent,
    ResourceEvent,
    ScriptEvent,
    SnapshotEvent,
    Topic,
    TraceEvent,
    topic_matches,
)


# --------------------------------------------------------------------------- #
# EventBus
# --------------------------------------------------------------------------- #



#: the fan-out item the running code is working on (an index path; empty outside a fan-out). The
#: executor sets it per item; the bus stamps it onto every event published meanwhile.
CURRENT_ITEM: ContextVar[tuple[int, ...]] = ContextVar("webclient_current_item", default=())

#: the PLAN STEP the running code is executing, as an address into the plan: the step's index in its
#: plan's step list (the ``get`` of a get+call pair), descending into a sub-plan by the arg it sits in --
#: ``("6", "kw:title", "0")`` is step 0 of the ``title=`` column of the extract at step 6. The executor
#: sets it; the bus stamps it (joined with "/") onto every event published meanwhile (``Event.step``),
#: so a page fetched, a request, an action or an error is attached to the step that caused it.
CURRENT_STEP: ContextVar[tuple[str, ...]] = ContextVar("webclient_current_step", default=())

#: the RUN the running code belongs to (an id the caller picks, e.g. the service's run id). Set it with
#: :func:`run_scope`; the bus stamps it onto every event published meanwhile (``Event.run_id``), so two
#: runs sharing one engine -- one bus -- can still be told apart (a trace keeps only its own run's).
CURRENT_RUN: ContextVar[str | None] = ContextVar("webclient_current_run", default=None)


@contextmanager
def run_scope(run_id: str) -> Iterator[str]:
    """Everything published inside is stamped ``run_id`` (the engine loop carries it into the
    coroutines it runs for this caller)."""
    token = CURRENT_RUN.set(run_id)
    try:
        yield run_id
    finally:
        CURRENT_RUN.reset(token)

class Subscription(BaseModel):
    id: str
    topic: Topic

    _bus: "EventBus | None" = PrivateAttr(default=None)

    def cancel(self) -> None:
        """Unsubscribe from the bus (idempotent -- a second cancel does nothing)."""
        if self._bus is not None:
            self._bus._remove(self.id)
            self._bus = None


class EventBus(BaseModel):
    """Shared pub/sub. Synchronous dispatch on the publisher's thread;
    handlers must not block and must not call sync facade methods.

    The bus stamps ``n`` (a global monotonic sequence), ``seq`` (monotonic per document
    id; a shared stream for events without one) and ``ts`` on every publish, and keeps a
    bounded HISTORY of recent events so a late subscriber (a websocket that reconnects, a
    trace writer attached mid-run) can :meth:`since` a cursor and catch up.
    """

    history: int = 10_000  # events kept for ``since`` (0 = keep none)

    _lock: "threading.RLock" = PrivateAttr(default_factory=threading.RLock)
    _subs: dict[str, tuple[Topic, dict[str, str | None], Callable[[Event], None]]] = (
        PrivateAttr(default_factory=dict)
    )
    _seq: dict[str | None, int] = PrivateAttr(default_factory=dict)
    _n: int = PrivateAttr(default=0)
    _recent: "deque[Event]" = PrivateAttr(default_factory=deque)
    #: document id -> the run that opened it: events a page publishes from outside the run's
    #: context (browser callbacks, pumps) belong to the run whose page it is
    _doc_run: "OrderedDict[str, str]" = PrivateAttr(default_factory=OrderedDict)

    def model_post_init(self, __context: Any) -> None:
        """Size the history ring to ``history``."""
        self._recent = deque(maxlen=max(0, self.history))

    @property
    def cursor(self) -> int:
        """The global sequence number of the last published event (0 before any)."""
        return self._n

    def since(self, n: int = 0, *, topic: Topic = "", run_id: str | None = None) -> list[Event]:
        """The retained events with a global sequence GREATER than ``n`` (optionally by topic
        prefix), oldest first -- the catch-up a resuming subscriber replays before going live.
        Bounded by ``history``: an older cursor gets what is still retained."""
        with self._lock:
            return [
                e for e in self._recent
                if (e.n or 0) > n and topic_matches(topic, e.topic)
                and (run_id is None or e.run_id == run_id)
            ]

    def publish(self, event: Event) -> None:
        """Stamp the event with a monotonic ``n`` (global) and ``seq`` (per document id) and a
        timestamp, retain it in the history ring, then dispatch it synchronously to every
        subscriber whose topic pattern and correlation filters match. Runs handlers on the
        publisher's thread."""
        with self._lock:
            key = event.document_id
            self._seq[key] = self._seq.get(key, 0) + 1
            event.seq = self._seq[key]
            self._n += 1
            event.n = self._n
            event.ts = time.time()
            if event.item is None:
                item = CURRENT_ITEM.get()
                if item:
                    event.item = list(item)
            if event.step is None:
                step = CURRENT_STEP.get()
                if step:
                    event.step = "/".join(step)
            if event.run_id is None:
                event.run_id = CURRENT_RUN.get() or (self._doc_run.get(key) if key else None)
            if event.run_id and key and self._doc_run.get(key) != event.run_id:
                self._doc_run[key] = event.run_id
                while len(self._doc_run) > 5_000:
                    self._doc_run.popitem(last=False)
            if self._recent.maxlen:
                self._recent.append(event)
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
        run_id: str | None = None,
    ) -> Subscription:
        """Handler fires for events whose topic matches ``topic`` by dotted
        prefix ("" matches everything) and every given correlation filter."""
        sub_id = uuid4().hex
        filters = {
            "session_id": session_id,
            "document_id": document_id,
            "plan_id": plan_id,
            "run_id": run_id,
        }
        with self._lock:
            self._subs[sub_id] = (topic, filters, handler)
        sub = Subscription(id=sub_id, topic=topic)
        sub._bus = self
        return sub

    def _remove(self, sub_id: str) -> None:
        """Drop a subscription by id (thread-safe) -- the unsubscribe behind ``Subscription.cancel``."""
        with self._lock:
            self._subs.pop(sub_id, None)


# --------------------------------------------------------------------------- #
# EventRegistry
# --------------------------------------------------------------------------- #


Upcaster = Callable[[dict[str, Any]], dict[str, Any]]


class EventRegistry(BaseModel):
    """Topic -> event class, for typed round-tripping over the wire / a trace and for
    resolving event types referenced in plans. Core events pre-registered;
    unknown topics resolve to the nearest registered ancestor (trailing
    segments dropped first, then the leading namespace), falling back to
    Event.

    Schema evolution is by UPCASTING on read (the event-sourcing discipline): an event
    stored at version ``v`` is transformed by the registered ``upcaster(topic, v)`` chain
    until it reaches the class's current ``version``, then validated -- so application code
    only ever handles the latest shape and old traces stay readable."""

    _by_topic: dict[str, type[Event]] = PrivateAttr(default_factory=dict)
    _upcasters: dict[tuple[str, int], Upcaster] = PrivateAttr(default_factory=dict)

    def model_post_init(self, __context: Any) -> None:
        """Pre-register the core event classes (and the rrweb chunk) so their topics resolve
        out of the box."""
        for cls in CORE_EVENTS:
            self.register(cls)
        from .rrweb import RRWebEvent

        self.register(RRWebEvent)

    def upcaster(self, topic: Topic, from_version: int) -> Callable[[Upcaster], Upcaster]:
        """Register a function that lifts a ``topic`` event dict from ``from_version`` to
        ``from_version + 1`` (chained by :meth:`load`)."""
        def wrap(fn: Upcaster) -> Upcaster:
            self._upcasters[(topic, from_version)] = fn
            return fn
        return wrap

    def load(self, data: dict[str, Any]) -> Event:
        """Rebuild a typed event from its wire / trace dict: resolve the class by topic, apply
        the upcaster chain from the stored ``version`` up to the class's current version, then
        validate. A version newer than known is loaded as-is (forward-compatible fields are
        ignored by the model)."""
        topic = str(data.get("topic", ""))
        cls = self.resolve(topic)
        current = int(cls.model_fields["version"].default)
        version = int(data.get("version", 1))
        while version < current:
            fn = self._upcasters.get((topic, version))
            if fn is None:  # no upcaster registered: the shape is assumed compatible
                break
            data = fn(dict(data))
            version += 1
            data["version"] = version
        return cls.model_validate(data)

    def register(self, cls: type[Event]) -> None:
        """Register an event class under its default ``topic`` (so the wire can rebuild it)."""
        topic = cls.model_fields["topic"].default
        if isinstance(topic, str) and topic:
            self._by_topic[topic] = cls

    def resolve(self, topic: Topic) -> type[Event]:
        """The event class for a topic: an exact match, else the nearest registered ancestor
        (trailing segments dropped first, then the leading namespace), falling back to ``Event``."""
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
    "ErrorEvent",
    "LoopEvent",
    "PipelineEvent",
    "ScriptEvent",
    "SnapshotEvent",
    "ResourceEvent",
    "TraceEvent",
    "CORE_EVENTS",
    "topic_matches",
    # machinery (defined here)
    "EventBus",
    "EventRegistry",
    "Subscription",
    "Upcaster",
]
