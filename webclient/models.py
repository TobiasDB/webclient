"""The cross-cutting **Event** taxonomy -- the pydantic event types the bus,
registry and backings across every core share.

This module depends on nothing but ``pydantic`` + ``typing`` (no cores, no
backings, no engine), so any module can import it at top level with no
circular-reference risk. The runtime machinery that *uses* these lives elsewhere
-- the event bus/registry in :mod:`webclient.events`, which re-exports these
names so ``from webclient.events import NetworkEvent`` keeps working. A core's own
value models now live with that core (its ``models.py``): the document's
``Element`` / ``Transport`` / ``Metadata`` / ``Structure`` / ``Signal`` facet
models in :mod:`webclient.core.document.models`.
"""

from __future__ import annotations

from typing import Any, Literal, TypeVar

from pydantic import BaseModel, ConfigDict

from .errors import WebError

# --------------------------------------------------------------------------- #
# Event taxonomy
#
# Topics are dotted strings matched by prefix: subscribing to "network" also
# receives "network.xhr". Plugin events subclass one of these core events and
# may introduce namespaced topics ("rrweb.dom.update").
# --------------------------------------------------------------------------- #

Topic = str


class Event(BaseModel):
    """The base of every event on the bus / in a trace. ``topic`` is the dotted kind; the bus
    stamps ``n`` (a global, monotonic sequence -- the resume cursor for ``since``), ``seq``
    (per document) and ``ts``. ``version`` is the event's SCHEMA version: a stored trace
    carries it, and :meth:`EventRegistry.load` upcasts an older shape on read (roadmap N3)."""

    topic: Topic
    version: int = 1  # the event schema version (upcast on read; see EventRegistry)
    source: str = "core"  # name of the emitting plugin
    n: int | None = None  # global monotonic sequence, stamped by the bus (the resume cursor)
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
    request: Any = None  # the Reference for this request
    status_code: int | None = None
    body: bytes | None = None
    resource_type: str | None = None  # browser sub-request kind: xhr/fetch/document/...
    method: str | None = None  # the HTTP method (GET/POST/...), for the correlation request list
    headers: dict[str, str] | None = None  # the RESPONSE headers when captured (a trace/HAR needs them)
    elapsed: float | None = None  # seconds the request took, when known
    index: int | None = None  # 1-based COMPLETION order among xhr/fetch (the phase counter the
    # DOM stamps key to); None for a non-correlated request
    t_s: float | None = None  # seconds since the FIRST request (relative float), for the timeline


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
    phase: str = "started"  # started / row / done / divergence
    detail: dict[str, Any] = {}


# -- the ledger / loops / pipelines / scripts / snapshots ---------------------- #


class ErrorEvent(Event):
    """An error the engine produced, published AT CREATION -- whether it was raised, returned
    as a not-ok document (RETURN policy), or swallowed by a fallback -- so no error ever
    disappears (roadmap N5). ``error`` is the problem-details :class:`WebError`, already bound
    to the ``op`` / ``subject`` it occurred on; ``raised`` says whether the caller saw it."""

    topic: Topic = "error"
    error: WebError
    raised: bool = False


class LoopEvent(Event):
    """One step of a bounded loop (crawl / resolve / interaction / query / paginate / locate):
    ``phase`` is ``round`` (an observation was taken), ``decision`` (the driver decided),
    ``done`` / ``stalled`` / ``budget`` / ``error`` (the verdict), or ``waiting`` / ``resumed``
    (a human-in-the-loop checkpoint). ``round`` is 1-based."""

    topic: Topic = "loop"
    loop: str = ""  # the loop's name ("crawl", "interaction loop", ...)
    phase: Literal["round", "decision", "done", "stalled", "budget", "error", "waiting", "resumed"] = "round"
    round: int = 0
    detail: dict[str, Any] = {}


class PipelineEvent(Event):
    """A pipeline stage boundary: ``phase`` ``enter`` / ``exit`` / ``gate`` (a validation
    verdict) / ``review`` / ``error``; ``detail`` carries the stage's summary."""

    topic: Topic = "pipeline"
    pipeline: str = ""
    stage: str = ""
    phase: Literal["enter", "exit", "gate", "review", "error"] = "enter"
    detail: dict[str, Any] = {}


class ScriptEvent(Event):
    """A page script ran: which named script, at which lifecycle ``phase``, and what it
    returned (summarised in ``detail``)."""

    topic: Topic = "script"
    script: str = ""
    phase: str = ""
    detail: dict[str, Any] = {}


class SnapshotEvent(Event):
    """A DOM / document snapshot -- the settled content of a document at a moment: after a
    fetch (``phase="fetch"``), a browser load (``"load"``) or an interaction (``"action"``).
    ``content`` is the captured bytes (a trace writer offloads it to an asset file and sets
    ``asset`` to its relative path); the static replay engine rebuilds a document from it."""

    topic: Topic = "snapshot"
    phase: Literal["fetch", "load", "action"] = "fetch"
    url: str = ""
    final_url: str = ""
    kind: str = "html"
    status_code: int = 0
    headers: dict[str, str] = {}
    encoding: str | None = None
    content: bytes | None = None
    asset: str = ""  # the relative asset path when the content was offloaded (a trace)
    tiers: list[str] = []


class ResourceEvent(Event):
    """A resource observation (pool leases, memory, store sizes) for the scalability work."""

    topic: Topic = "resource"
    detail: dict[str, Any] = {}


#: the pre-registered core event classes (see ``EventRegistry``).
CORE_EVENTS: tuple[type[Event], ...] = (
    NetworkEvent,
    NavigationEvent,
    DOMEvent,
    DOMUpdateEvent,
    ActionEvent,
    ConsoleEvent,
    PlanEvent,
    ErrorEvent,
    LoopEvent,
    PipelineEvent,
    ScriptEvent,
    SnapshotEvent,
    ResourceEvent,
)


def topic_matches(pattern: Topic, topic: Topic) -> bool:
    """Whether ``topic`` matches ``pattern`` by dotted prefix (``""`` matches all)."""
    return not pattern or topic == pattern or topic.startswith(pattern + ".")


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
    "ErrorEvent",
    "LoopEvent",
    "PipelineEvent",
    "ScriptEvent",
    "SnapshotEvent",
    "ResourceEvent",
    "CORE_EVENTS",
    "topic_matches",
]
