"""webclient -- declarative web client.

M1 exposes the pure core (Reference, Document + typed views, Node) and the
event taxonomy. The interface spec lives in /models.py at the repo root;
PLAN.md maps every remaining name to its milestone.
"""
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
    FetchEvent,
    NavigationEvent,
    NetworkEvent,
    Topic,
    XHREvent,
)
from .models import (
    BinaryDocument,
    Document,
    FetchError,
    HTMLDocument,
    JSONDocument,
    Node,
    Reference,
    XMLDocument,
)

__all__ = [
    "ActionEvent", "AssetEvent", "ConsoleEvent", "DOMEvent", "DOMLoadEvent",
    "DOMSnapshotEvent", "DOMUnloadEvent", "DOMUpdateEvent", "Event",
    "FetchEvent", "NavigationEvent", "NetworkEvent", "Topic", "XHREvent",
    "BinaryDocument", "Document", "FetchError", "HTMLDocument",
    "JSONDocument", "Node", "Reference", "XMLDocument",
]
