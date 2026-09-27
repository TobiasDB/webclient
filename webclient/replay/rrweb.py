"""The rrweb translation layer -- our one event stream <-> rrweb's event list, both ways.

**Forward** (:func:`to_rrweb`): the whole stream becomes ONE rrweb event list a single player
drives. The DOM comes from the recorded chunks when the run had the recorder on (a browser
tier under a trace), else from the document snapshots, SYNTHESISED into rrweb's own
``Meta`` + ``FullSnapshot`` (an HTML -> serialized-node walk), so a static run replays in the
same player. Every other event (network, action, console, loop, pipeline, error, script,
resource, plan) becomes an rrweb ``Custom`` event tagged with its topic and carrying its
wire dict -- rrweb-player shows them on its progress bar and emits them as it plays, which is
how the UI's network / console / loop panes follow the same cursor as the DOM.

**Back** (:func:`from_rrweb`): an rrweb recording (ours, or one made elsewhere) maps into our
events: ``FullSnapshot`` -> :class:`SnapshotEvent` (the node tree serialised back to HTML),
mutations -> :class:`DOMUpdateEvent`, clicks / input / scroll -> :class:`ActionEvent`, and a
``Custom`` event whose payload is one of ours -> that event, through the registry.

rrweb event types: 0 DomContentLoaded, 1 Load, 2 FullSnapshot, 3 IncrementalSnapshot,
4 Meta, 5 Custom. Incremental sources: 0 Mutation, 2 MouseInteraction, 3 Scroll,
4 ViewportResize, 5 Input. Node types: 0 Document, 1 DocumentType, 2 Element, 3 Text,
4 CDATA, 5 Comment.
"""

from __future__ import annotations

from html import escape
from typing import TYPE_CHECKING, Any
from urllib.parse import urljoin

from ..models import ActionEvent, DOMEvent, DOMUpdateEvent, Event, SnapshotEvent, TraceEvent

if TYPE_CHECKING:
    from ..events import EventRegistry

__all__ = ["to_rrweb", "from_rrweb", "html_to_node", "node_to_html"]

FULL, INCREMENTAL, META, CUSTOM = 2, 3, 4, 5
_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}
_URL_ATTRS = {"src", "href", "poster", "action"}


# --------------------------------------------------------------------------- #
# forward: our stream -> rrweb
# --------------------------------------------------------------------------- #

def to_rrweb(events: "list[Event]", *, document_id: "str | None" = None, custom: bool = True,
             width: "int | None" = None, height: "int | None" = None) -> "list[dict[str, Any]]":
    """See the module doc. ``document_id`` narrows to one document; ``custom=False`` gives
    the DOM alone. Sorted by timestamp (stable), so recorded chunks and synthesised
    snapshots interleave with the custom events in run order."""
    from ..rrweb import VIEWPORT
    from ..trace import wire

    width, height = width or VIEWPORT[0], height or VIEWPORT[1]
    recorded = {e.document_id for e in events if e.topic == "rrweb"}
    out: list[dict[str, Any]] = []
    for e in events:
        if document_id is not None and e.document_id != document_id:
            continue
        ms = int((e.ts or 0.0) * 1000)
        if e.topic == "rrweb":
            out.extend(getattr(e, "events", []) or [])
        elif isinstance(e, SnapshotEvent) and e.kind == "html" and e.content and e.document_id not in recorded:
            href = e.final_url or e.url
            out.append({"type": META, "data": {"href": href, "width": width, "height": height}, "timestamp": ms - 1})
            out.append({"type": FULL, "data": {"node": html_to_node(e.content, base=href),
                                                "initialOffset": {"top": 0, "left": 0}}, "timestamp": ms})
            if custom:
                out.append(_custom(e, wire, ms))
        elif custom and not isinstance(e, TraceEvent):
            out.append(_custom(e, wire, ms))
    out.sort(key=lambda r: (r.get("timestamp", 0), 1 if r["type"] == CUSTOM else 0))  # DOM first at a tie
    # a player needs Meta + FullSnapshot FIRST: pull the first Meta and the first FullSnapshot ahead
    # of any custom event that fired at or before their time (a pool lease, a plan start, a navigation
    # sharing the synthesised Meta's millisecond) so replay always starts from a full document. The
    # FullSnapshot is pulled from WHEREVER it landed -- a tie can sort a custom event between the pair.
    mi = next((k for k, r in enumerate(out) if r["type"] == META), None)
    if mi is not None:
        fi = next((k for k, r in enumerate(out) if r["type"] == FULL), None)
        idx = [k for k in (mi, fi) if k is not None]
        if idx != list(range(len(idx))):  # not already leading, in order
            head = [out[k] for k in idx]
            t0 = out[0].get("timestamp", 0)
            for k, r in enumerate(head):
                r["timestamp"] = t0 - len(head) + k
            out = head + [r for k, r in enumerate(out) if k not in idx]
    return out


def _custom(e: Event, wire: Any, ms: int) -> dict[str, Any]:
    return {"type": CUSTOM, "data": {"tag": e.topic, "payload": wire(e)}, "timestamp": ms}


def html_to_node(content: bytes, *, base: str = "") -> dict[str, Any]:
    """Serialise HTML into rrweb's node tree (ids assigned in document order, URLs made
    absolute against ``base``, scripts dropped -- rrweb never re-executes them either)."""
    from ..dom.parse import parse_html

    root = parse_html(content)
    counter = [1]

    def nid() -> int:
        n = counter[0]
        counter[0] += 1
        return n

    def walk(el: Any) -> "dict[str, Any] | None":
        tag = el.tag if isinstance(el.tag, str) else None
        if tag is None:  # a comment / processing instruction
            text = el.text or ""
            node: dict[str, Any] = {"type": 5, "textContent": text, "id": nid()}
        else:
            tag = tag.lower()
            if tag == "script":
                return None
            attrs = {k: (urljoin(base, v) if k in _URL_ATTRS and base else v) for k, v in el.attrib.items()}
            node = {"type": 2, "tagName": tag, "attributes": attrs, "childNodes": [], "id": nid()}
            if el.text:
                node["childNodes"].append({"type": 3, "textContent": el.text, "id": nid()})
            for child in el:
                sub = walk(child)
                if sub is not None:
                    node["childNodes"].append(sub)
                if child.tail:
                    node["childNodes"].append({"type": 3, "textContent": child.tail, "id": nid()})
        return node

    doc: dict[str, Any] = {"type": 0, "childNodes": [], "id": nid()}
    if root is None:
        return doc
    html = root if (isinstance(root.tag, str) and root.tag.lower() == "html") else root.getroottree().getroot()
    doc["childNodes"].append({"type": 1, "name": "html", "publicId": "", "systemId": "", "id": nid()})
    top = walk(html)
    if top is not None:
        doc["childNodes"].append(top)
    return doc


# --------------------------------------------------------------------------- #
# back: rrweb -> our stream
# --------------------------------------------------------------------------- #

def node_to_html(node: dict[str, Any]) -> str:
    """Serialise rrweb's node tree back to HTML."""
    t = node.get("type")
    if t == 0:
        return "".join(node_to_html(c) for c in node.get("childNodes", []))
    if t == 1:
        return f"<!DOCTYPE {node.get('name', 'html')}>"
    if t == 3:
        return escape(str(node.get("textContent", "")), quote=False)
    if t == 4:
        return f"<![CDATA[{node.get('textContent', '')}]]>"
    if t == 5:
        return f"<!--{node.get('textContent', '')}-->"
    tag = str(node.get("tagName", "div")).lower()
    attrs = "".join(f' {k}="{escape(str(v), quote=True)}"' for k, v in (node.get("attributes") or {}).items()
                    if v is not False and v is not None)
    if tag in _VOID:
        return f"<{tag}{attrs}>"
    inner = "".join(node_to_html(c) for c in node.get("childNodes", []))
    return f"<{tag}{attrs}>{inner}</{tag}>"


def from_rrweb(rr: "list[dict[str, Any]]", *, document_id: "str | None" = None,
               registry: "EventRegistry | None" = None) -> "list[Event]":
    """See the module doc. Timestamps (ms) become ``ts``; ``document_id`` is stamped on the
    events that lack one."""
    from ..events import EventRegistry

    reg = registry or EventRegistry()
    out: list[Event] = []
    href = ""
    for r in rr:
        t, data, ts = r.get("type"), r.get("data") or {}, float(r.get("timestamp", 0)) / 1000.0
        ev: "Event | None" = None
        if t == META:
            href = str(data.get("href", "") or href)
            ev = DOMEvent(topic="dom.meta", detail={"href": href, "width": data.get("width"), "height": data.get("height")})
        elif t == FULL:
            ev = SnapshotEvent(phase="load", url=href, final_url=href, kind="html", status_code=200,
                               content=node_to_html(data.get("node") or {}).encode("utf-8"))
        elif t == INCREMENTAL:
            src = data.get("source")
            if src == 0:
                for a in data.get("adds", []) or []:
                    out.append(_stamp(DOMUpdateEvent(kind="added", detail={"id": a.get("node", {}).get("id"), "parent": a.get("parentId")}), ts, document_id))
                for rm in data.get("removes", []) or []:
                    out.append(_stamp(DOMUpdateEvent(kind="removed", detail={"id": rm.get("id"), "parent": rm.get("parentId")}), ts, document_id))
                for at in data.get("attributes", []) or []:
                    out.append(_stamp(DOMUpdateEvent(kind="attribute", detail={"id": at.get("id"), "attributes": at.get("attributes")}), ts, document_id))
                for tx in data.get("texts", []) or []:
                    out.append(_stamp(DOMUpdateEvent(kind="text", detail={"id": tx.get("id"), "value": tx.get("value")}), ts, document_id))
                continue
            if src == 2 and data.get("type") == 2:
                ev = ActionEvent(action="click", args={"id": data.get("id"), "x": data.get("x"), "y": data.get("y")})
            elif src == 5:
                ev = ActionEvent(action="write", args={"id": data.get("id"), "text": data.get("text")})
            elif src == 3:
                ev = ActionEvent(action="scroll", args={"id": data.get("id"), "x": data.get("x"), "y": data.get("y")})
            else:
                ev = DOMEvent(topic="dom.rrweb", detail=dict(data))
        elif t == CUSTOM:
            payload = data.get("payload")
            if isinstance(payload, dict) and payload.get("topic"):
                try:
                    ev = reg.load(dict(payload))
                except Exception:  # noqa: BLE001 - a foreign payload: keep it as a plain event
                    ev = Event(topic=str(data.get("tag", "custom")))
            else:
                ev = Event(topic=str(data.get("tag", "custom")))
        if ev is not None:
            ev.source = ev.source if ev.source != "core" else "rrweb"
            out.append(_stamp(ev, ts, document_id))
    return out


def _stamp(ev: Event, ts: float, document_id: "str | None") -> Event:
    if ev.ts is None:
        ev.ts = ts
    if document_id and not ev.document_id:
        ev.document_id = document_id
    return ev
