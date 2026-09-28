"""The event bus: a minimal, synchronous pub/sub over dotted topics.

The kernel defines only the :class:`Event` base and the :class:`EventBus`. Every layer
defines its own event types (``fetch`` a ``FetchEvent``, ``parse`` a ``ParseEvent``, ...) and
publishes them on a shared bus, so a trace or UI can observe any layer without the kernel
knowing those types exist. Delivery is synchronous and in subscription order; a subscriber
that raises does not stop the others (its error is swallowed -- the bus is observation, not
control flow). Matching is by dotted-topic prefix: a subscriber on ``"fetch"`` sees
``"fetch"`` and ``"fetch.retry"``; a subscriber on ``""`` sees everything.
"""

from __future__ import annotations

import contextvars
import time
from collections.abc import Callable

from pydantic import BaseModel, Field


class Event(BaseModel):
    """Base for anything published on the bus. ``topic`` routes it (a dotted string);
    subclasses add their own fields. ``ts`` is set on creation; ``source`` optionally names
    what emitted it (a document id, a session id)."""

    topic: str
    ts: float = Field(default_factory=time.time)
    source: str | None = None


def topic_matches(prefix: str, topic: str) -> bool:
    """Whether ``topic`` is under ``prefix`` by dotted segments (``""`` matches everything)."""
    return not prefix or topic == prefix or topic.startswith(prefix + ".")


class Subscription:
    """A live subscription; call it (or use it as a context manager) to unsubscribe."""

    def __init__(self, bus: "EventBus", prefix: str, handler: "Callable[[Event], None]") -> None:
        self._bus, self.prefix, self.handler = bus, prefix, handler

    def __call__(self) -> None:
        self._bus._remove(self)

    def __enter__(self) -> "Subscription":
        return self

    def __exit__(self, *exc: object) -> None:
        self()


class EventBus:
    """Synchronous pub/sub. ``subscribe`` a handler to a topic prefix; ``publish`` delivers an
    event to every matching handler, in order, swallowing handler errors."""

    def __init__(self) -> None:
        self._subs: list[Subscription] = []

    def subscribe(self, prefix: str, handler: "Callable[[Event], None]") -> Subscription:
        sub = Subscription(self, prefix, handler)
        self._subs.append(sub)
        return sub

    def publish(self, event: Event) -> None:
        for sub in tuple(self._subs):  # a copy: a handler may (un)subscribe mid-dispatch
            if topic_matches(sub.prefix, event.topic):
                try:
                    sub.handler(event)
                except Exception:  # a bus subscriber is an observer; it never breaks the emitter
                    pass

    def _remove(self, sub: Subscription) -> None:
        try:
            self._subs.remove(sub)
        except ValueError:
            pass


# -- the ambient bus: layers publish without threading a bus through every call ---------
_CURRENT: "contextvars.ContextVar[EventBus | None]" = contextvars.ContextVar("web_bus", default=None)


def emit(event: Event) -> None:
    """Publish ``event`` to the ambient trace bus if one is active in this context, else do
    nothing. Layers call this to report what they do (a fetch, a retry, a page); it stays a no-op
    -- and free -- unless a :class:`Trace` (or :func:`using`) has installed a bus. Works across
    ``await`` because the bus lives in a ``ContextVar``."""
    bus = _CURRENT.get()
    if bus is not None:
        bus.publish(event)


class _Using:
    def __init__(self, bus: EventBus) -> None:
        self._bus = bus
        self._token: "contextvars.Token[EventBus | None] | None" = None

    def __enter__(self) -> EventBus:
        self._token = _CURRENT.set(self._bus)
        return self._bus

    def __exit__(self, *exc: object) -> None:
        if self._token is not None:
            _CURRENT.reset(self._token)


def using(bus: EventBus) -> _Using:
    """Install ``bus`` as the ambient bus for the ``with`` scope (so :func:`emit` reaches it)."""
    return _Using(bus)


class Trace:
    """Collect the events emitted within a scope: ``with Trace() as t: ...; t.events``. Also a bus,
    so a caller can ``t.subscribe(prefix, handler)`` for live handling."""

    def __init__(self) -> None:
        self.bus = EventBus()
        self.events: list[Event] = []
        self.bus.subscribe("", self.events.append)
        self._token: "contextvars.Token[EventBus | None] | None" = None

    def subscribe(self, prefix: str, handler: "Callable[[Event], None]") -> Subscription:
        return self.bus.subscribe(prefix, handler)

    def __enter__(self) -> "Trace":
        self._token = _CURRENT.set(self.bus)
        return self

    def __exit__(self, *exc: object) -> None:
        if self._token is not None:
            _CURRENT.reset(self._token)


__all__ = ["Event", "EventBus", "Subscription", "topic_matches", "emit", "using", "Trace"]

