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

import time
from collections.abc import Callable
from typing import Any

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

    def __exit__(self, *exc: Any) -> None:
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


__all__ = ["Event", "EventBus", "Subscription", "topic_matches"]
