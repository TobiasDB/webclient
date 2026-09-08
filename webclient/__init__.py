"""webclient -- declarative web client.

M1: pure core (Reference, Document + typed views, Node) and the event
taxonomy. M2: WebClient (http fetch via the engine loop + ClientPool),
EventBus/EventRegistry, plugin framework, core network capture and core
renderers. The interface spec lives in /models.py at the repo root; PLAN.md
maps every remaining name to its milestone.
"""
from .client import WebClient, default_client
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
from .models import (
    BinaryDocument,
    Document,
    Element,
    FetchError,
    HTMLDocument,
    JSONDocument,
    Node,
    Proxy,
    Reference,
    Script,
    XMLDocument,
)
from .lazy import Expr, Lazy, QueryPlan, col, lit, q
from .lazy.executor import ExecutionGraph, ExecutionStep, Executor, RunStats
from .live import LiveDocument, LiveNode
from .models import OnError
from .plugins.base import Plugin, Renderer, Surface, SurfaceKind
from .pool import ClientPool, Lease, PoolStats
from .session import Session

__all__ = [
    "ActionEvent", "AssetEvent", "ConsoleEvent", "DOMEvent", "DOMLoadEvent",
    "DOMSnapshotEvent", "DOMUnloadEvent", "DOMUpdateEvent", "Event",
    "EventBus", "EventRegistry", "FetchEvent", "NavigationEvent",
    "NetworkEvent", "Subscription", "Topic", "XHREvent",
    "BinaryDocument", "Document", "Element", "FetchError", "HTMLDocument",
    "JSONDocument", "Node", "Proxy", "Reference", "Script", "XMLDocument",
    "Plugin", "Renderer", "Surface", "SurfaceKind",
    "ClientPool", "Lease", "PoolStats", "Session",
    "LiveDocument", "LiveNode",
    "Expr", "Lazy", "QueryPlan", "OnError", "col", "lit", "q",
    "Executor", "ExecutionGraph", "ExecutionStep", "RunStats",
    "WebClient", "default_client",
]
