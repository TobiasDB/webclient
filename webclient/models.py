"""Shared pure-data models -- the pydantic value types the cores, backings and
surfaces all pass around: the **Event** taxonomy and the **Summary** facets.

This module depends on nothing but ``pydantic`` + ``typing`` (no cores, no
backings, no engine), so any module can import it at top level with no
circular-reference risk. The runtime machinery that *uses* these models lives
elsewhere -- the event bus/registry in :mod:`webclient.events`, the summary
facet backings in :mod:`webclient.core.document.summary` -- and imports from
here. ``webclient.events`` / ``webclient.summary`` re-export these names, so the
existing import paths keep working.
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


# --------------------------------------------------------------------------- #
# Content blocks
#
# The "elements" representation of a document -- the typed content blocks a
# selection backing (html/json) produces from the parsed tree. A pure value
# type, exported to users as ``webclient.Element``.
# --------------------------------------------------------------------------- #


class Element(BaseModel):
    """A typed content block -- the "elements" representation of a document."""

    id: str = ""
    type: str = "text"
    text: str = ""
    parent_id: str | None = None
    metadata: dict[str, Any] = {}


# --------------------------------------------------------------------------- #
# Search
#
# A search hit -- the structured shape an agent or human reads back from
# ``client.search(query)``: title / url / description, all as the search
# provider gave them, plus the 1-based rank on the results page.
# --------------------------------------------------------------------------- #


class SearchResult(BaseModel):
    """One search hit: ``title`` / ``url`` / ``description`` as the provider gave
    them, plus ``rank`` (1-based position on the results page)."""

    rank: int = 0
    title: str = ""
    url: str = ""
    description: str = ""


# --------------------------------------------------------------------------- #
# Summary facets
#
# A summary is a *shape*, not a data dump -- headers, cookies and page metadata
# are reported as KEYS / TYPES, not values. It is assembled from independent
# facet backings, each an optional section (``None`` when not requested / not
# applicable). Populated by ``webclient.core.document.summary``.
# --------------------------------------------------------------------------- #

#: the facet sections, in order; the selector for ``summary(include=...)``.
FACETS = ("transport", "metadata", "structure", "runtime", "probe")


class TocEntry(BaseModel):
    level: int
    text: str


class Form(BaseModel):
    method: str = "get"
    action: str | None = None
    field_names: list[str] = []


class XhrCall(BaseModel):
    method: str
    url: str


class Transport(BaseModel):
    """Transport facts -- free from any resolved document (values only for the
    few that *are* the summary; headers/cookies are key lists)."""

    final_url: str
    status_code: int
    ok: bool
    kind: str
    redirect_chain: list[str] = []
    duration_ms: float | None = None
    content_type: str | None = None
    encoding: str | None = None
    size_bytes: int | None = None
    header_keys: list[str] = []
    set_cookie_keys: list[str] = []
    server: str | None = None
    cdn: str | None = None
    region: str | None = None


class Metadata(BaseModel):
    """Head / schema metadata -- title/description are values; og and JSON-LD are
    reported as key/type names."""

    title: str | None = None
    description: str | None = None
    lang: str | None = None
    canonical_url: str | None = None
    schema_types: list[str] = []
    og_keys: list[str] = []
    page_type: str | None = None
    feeds: list[str] = []
    sitemap_url: str | None = None


class Structure(BaseModel):
    """Body shape -- counts, a table of contents, forms (as field-name lists) and
    detected pagination."""

    toc: list[TocEntry] = []
    word_count: int | None = None
    reading_time_min: int | None = None
    main_content_present: bool | None = None
    links_internal: int = 0
    links_external: int = 0
    link_sample: list[str] = []
    forms: list[Form] = []
    pagination: str | None = None
    media_img: int = 0
    media_video: int = 0


class Runtime(BaseModel):
    """Browser-only signals, read from captured DOM/network events (``None`` on a
    static fetch)."""

    is_spa: bool | None = None
    framework: str | None = None
    uses_xhr: bool | None = None
    uses_fetch: bool | None = None
    xhr_endpoints: list[XhrCall] = []
    dynamic_elements: list[str] = []


class Probe(BaseModel):
    """What an auto-resolve had to escalate to (``None`` unless the resolution
    recorded it) -- the read-side of the resiliency layer."""

    was_browser_required: bool | None = None
    was_proxy_required: bool | None = None
    anti_bot: str | None = None
    js_required: bool | None = None
    paywall: bool | None = None
    login_wall: bool | None = None
    render_blocked: bool | None = None


class Summary(BaseModel):
    """A page overview: each facet an optional section (``None`` when not
    requested / not applicable)."""

    transport: Transport | None = None
    metadata: Metadata | None = None
    structure: Structure | None = None
    runtime: Runtime | None = None
    probe: Probe | None = None


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
    # content blocks
    "Element",
    # search
    "SearchResult",
    # summary
    "FACETS",
    "TocEntry",
    "Form",
    "XhrCall",
    "Transport",
    "Metadata",
    "Structure",
    "Runtime",
    "Probe",
    "Summary",
]
