"""The cross-cutting **Event** taxonomy -- the pydantic event types the bus,
registry and backings across every core share.

This module depends on nothing but ``pydantic`` + ``typing`` (no cores, no
backings, no engine), so any module can import it at top level with no
circular-reference risk. The runtime machinery that *uses* these lives elsewhere
-- the event bus/registry in :mod:`webclient.events`, which re-exports these
names so ``from webclient.events import NetworkEvent`` keeps working. A core's own
value models now live with that core (its ``models.py``): the document's
``Element`` / ``Summary`` facets in :mod:`webclient.core.document.models`, the
client's ``SearchResult`` in :mod:`webclient.core.client.models`.
"""

from __future__ import annotations

from typing import Any, Literal, TypeVar

from pydantic import BaseModel, ConfigDict

# --------------------------------------------------------------------------- #
# Event taxonomy
#
# Topics are dotted strings matched by prefix: subscribing to "network" also
# receives "network.xhr". Plugin events subclass one of these core events and
# may introduce namespaced topics ("rrweb.dom.update").
# --------------------------------------------------------------------------- #

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


#: an Event subtype, so ``events_of(NavigationEvent)`` narrows to that subtype.
E = TypeVar("E", bound=Event)


# -- network ---------------------------------------------------------------- #


class NetworkEvent(Event):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    topic: Topic = "network"
    request: Any = None  # the ReferenceCore for this request
    status_code: int | None = None
    body: bytes | None = None
    resource_type: str | None = None  # browser sub-request kind: xhr/fetch/document/...


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


#: the pre-registered core event classes (see ``EventRegistry``).
CORE_EVENTS: tuple[type[Event], ...] = (
    NetworkEvent,
    NavigationEvent,
    DOMEvent,
    DOMUpdateEvent,
    ActionEvent,
    ConsoleEvent,
)


def topic_matches(pattern: Topic, topic: Topic) -> bool:
    """Whether ``topic`` matches ``pattern`` by dotted prefix (``""`` matches all)."""
    return not pattern or topic == pattern or topic.startswith(pattern + ".")


#: back-compat alias (the private name the bus/backings imported).
_topic_matches = topic_matches


# A core's own data models now live with that core (its ``models.py``): the
# document's ``Element`` + ``Summary`` facets in :mod:`webclient.core.document.models`
# (re-exported by :mod:`webclient.summary`), the client's ``SearchResult`` in
# :mod:`webclient.core.client.models`. This module stays the cross-cutting event
# taxonomy -- what the bus/registry and backings across every core share.


__all__ = [
    # events
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
]
