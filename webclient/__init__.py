"""webclient -- declarative web client.

M1: pure core (Reference, Document + typed views, Node) and the event
taxonomy. M2: WebClient (http fetch via the engine loop + ClientPool),
EventBus/EventRegistry, plugin framework, core network capture and core
renderers. The interface spec lives in /spec.py at the repo root; PLAN.md
maps every remaining name to its milestone.
"""
from typing import TYPE_CHECKING

from .core.engine import Proxy
from .core.engine import (
    AsyncWebClient,
    SearchEngine,
    WebClient,
    WebClientCore,
    default_client,
)
from .events import (
    ActionEvent,
    AssetEvent,
    ConsoleEvent,
    DOMEvent,
    DOMLoadEvent,
    DOMSnapshotEvent,
    DOMUnloadEvent,
    DOMUpdateEvent,
    Event,
    EventBus,
    EventRegistry,
    FetchEvent,
    NavigationEvent,
    NetworkEvent,
    Subscription,
    Topic,
    XHREvent,
)
from .core.document import (
    LiveDocument,
    LiveNode,
    Collection,
    Document,
    Element,
    FetchError,
    Field,
    Reference,
    Script,
    WebBase,
    WebError,
)
from .core.base import IGNORE, RAISE, RETURN, OpError, UnsupportedOperation
from .core.expr import (Plan, field, filter, from_plan, is_empty, is_ok, lazy,
                       reference, when)
if TYPE_CHECKING:
    from .core.expr import doc, many, ref
from .core.executor import PlanEvent
from .plugins.base import Plugin, Renderer, Surface, SurfaceKind
from .pool import ClientPool, Lease, PoolStats
from .core.remote import RemoteError, RemoteWebClient, RemoteWebClientCore
from .core.engine import Session


def __getattr__(name: str):
    """doc / many / ref are installed after models loads; read them
    live (see webclient.core.expr)."""
    if name in ("doc", "many", "ref"):
        from .core import expr as _expr
        return getattr(_expr, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

__all__ = [
    "ActionEvent", "AssetEvent", "ConsoleEvent", "DOMEvent", "DOMLoadEvent",
    "DOMSnapshotEvent", "DOMUnloadEvent", "DOMUpdateEvent", "Event",
    "EventBus", "EventRegistry", "FetchEvent", "NavigationEvent",
    "NetworkEvent", "Subscription", "Topic", "XHREvent",
    "Document", "Element", "FetchError", "Proxy", "Reference", "Script",
    "WebBase", "WebError", "Field", "Collection",
    "IGNORE", "RETURN", "RAISE", "OpError", "UnsupportedOperation",
    "Plugin", "Renderer", "Surface", "SurfaceKind",
    "ClientPool", "Lease", "PoolStats", "Session",
    "LiveDocument", "LiveNode",
    "Plan", "doc", "many", "ref", "field", "filter", "is_empty", "is_ok",
    "lazy", "when", "reference",
    "from_plan", "PlanEvent",
    "RemoteWebClient", "RemoteWebClientCore", "RemoteError",
    "WebClient", "AsyncWebClient", "WebClientCore", "SearchEngine",
    "default_client",
]
