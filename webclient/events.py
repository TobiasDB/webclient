"""Core event taxonomy (models only -- the bus and registry land in M2).

Topics are dotted strings matched by prefix: subscribing to "network" also
receives "network.xhr". Plugin events subclass one of these core events and
may introduce namespaced topics ("rrweb.dom.update").
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal, TypeVar

from pydantic import BaseModel

if TYPE_CHECKING:
    from .models import Reference

Topic = str


class Event(BaseModel):
    topic: Topic
    source: str = "core"             # name of the emitting plugin
    seq: int | None = None           # per-document counter, stamped by the bus
    ts: float | None = None          # stamped by the bus
    # correlation ids -- overwritten by Surface.emit (ISSUES #15)
    session_id: str | None = None
    document_id: str | None = None
    plan_id: str | None = None
    node_id: str | None = None       # stable node identity, stamped by the
                                     # capture plugin (enables LiveNode
                                     # event narrowing; ISSUES #9)


E = TypeVar("E", bound=Event)


# -- network ---------------------------------------------------------------- #

class NetworkEvent(Event):
    topic: Topic = "network"
    request: Reference
    status_code: int | None = None
    body: bytes | None = None


class XHREvent(NetworkEvent):
    topic: Topic = "network.xhr"


class FetchEvent(NetworkEvent):
    topic: Topic = "network.fetch"


class NavigationEvent(NetworkEvent):
    topic: Topic = "network.navigation"


class AssetEvent(NetworkEvent):
    topic: Topic = "network.asset"
    asset_type: str = ""             # css / js / image / font / media


# -- dom -------------------------------------------------------------------- #

class DOMEvent(Event):
    topic: Topic = "dom"
    selector: str | None = None
    detail: dict[str, Any] = {}


class DOMLoadEvent(DOMEvent):
    topic: Topic = "dom.load"


class DOMUpdateEvent(DOMEvent):
    topic: Topic = "dom.update"
    kind: Literal["added", "removed", "attribute", "text"] = "added"


class DOMUnloadEvent(DOMEvent):
    topic: Topic = "dom.unload"


class DOMSnapshotEvent(DOMEvent):
    """Full-DOM checkpoint so stream consumers resync instead of replaying
    (and diverging from) a full incremental history."""

    topic: Topic = "dom.snapshot"
    snapshot: dict[str, Any] = {}
    digest: str = ""


# -- interaction & console --------------------------------------------------- #

class ActionEvent(Event):
    topic: Topic = "action"
    action: str                      # "click", "write", "scroll", ...
    args: dict[str, Any] = {}


class ConsoleEvent(Event):
    topic: Topic = "console"
    level: Literal["log", "info", "warning", "error"]
    text: str
