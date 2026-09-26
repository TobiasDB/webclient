"""The page's NETWORK, joined to the DOM it built: which requests a browser load made (when, how
long, from which frame, how big), and for each data request (xhr / fetch) the page nodes it most
likely produced -- read off the correlation substrate (the XHR timeline + the per-node phase stamps,
:mod:`.correlate`), content-matched against the response bodies when they were captured.

Attribution is inference (see :mod:`.correlate`): a node is tied to the request(s) that completed
just before it appeared, narrowed by matching its text against the bodies. ``confidence`` says how
sure; a node with many candidates is left out rather than guessed.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from pydantic import BaseModel

from ...dom import norm

if TYPE_CHECKING:
    from . import Document

#: the most nodes listed per request (a request that renders a 500-row table lists its first rows)
_NODES_MAX = 40
#: a node tied to more candidate requests than this is ambiguous: not listed
_CANDIDATES_MAX = 3
#: the most of a body a view carries (the full body is matched; a view shows its head)
_VIEW_BODY_MAX = 262_144
#: the JSON leaves read per body, and the shortest value worth matching (shorter ones are everywhere)
_LEAVES_MAX = 60_000
_VALUE_MIN = 4
#: elements whose text never shows data
_SKIP = frozenset({"script", "style", "noscript", "template", "head", "title", "meta", "link", "svg", "path"})


class NetNode(BaseModel):
    """A page node a request (likely) produced: where it is (a CSS path from the root), its text, and
    -- when its text IS a value of the response -- which field (``jobs[3].title``)."""

    selector: str
    text: str = ""
    field: str | None = None  # the response field whose value the node shows (content-matched)
    confidence: float | None = None  # 0..1: 1 = only this response has that value / the only candidate


class NetRequest(BaseModel):
    """One request of the page's load."""

    n: int  # its order by start time (1 = first)
    method: str = "GET"
    url: str = ""
    status: int | None = None
    type: str = ""  # document / script / stylesheet / image / font / xhr / fetch / …
    content_type: str = ""
    at: float | None = None  # seconds after the load's first request started
    elapsed: float | None = None  # seconds it took
    size: int | None = None  # response bytes (when known)
    frame: str = ""  # the frame that made it when not the page itself (an embedded widget)
    data: bool = False  # an xhr / fetch: the page asking for data
    phase: int | None = None  # its completion index among data requests (what the DOM stamps key to)
    produced: list[NetNode] = []  # the nodes it (likely) put on the page
    body: str | None = None  # a data request's body (text / JSON, capped) -- the full view only
    truncated: bool = False
    matched: int = 0  # how many of the page's nodes show one of its values (content-matched)


class NetworkView(BaseModel):
    """A document's network: its requests (start order) with what each produced, a count by type,
    how many stamped nodes were there before any data request (server-rendered or pure script) vs
    after one, and a line for a reader."""

    url: str = ""
    document_id: str = ""
    requests: list[NetRequest] = []
    by_type: dict[str, int] = {}
    static_nodes: int = 0  # nodes that appeared before any data request completed
    data_nodes: int = 0  # nodes tied to a data request
    summary: str = ""


def network_view(core: "Document", *, bodies: bool = True) -> NetworkView:
    """The :class:`NetworkView` of ``core`` (see the module). ``bodies=False`` leaves the data
    bodies out (the trace view: they are in the stream already)."""
    facts = sorted(core._requests, key=lambda f: f.get("started") or 0.0)
    page = core.final_url or core.url
    t0 = next((f["started"] for f in facts if f.get("started")), None)
    reqs: list[NetRequest] = []
    for i, f in enumerate(facts, 1):
        frame = str(f.get("frame") or "")
        reqs.append(NetRequest(
            n=i, method=str(f.get("method") or "GET"), url=str(f.get("url") or ""), status=f.get("status"),
            type=str(f.get("resource_type") or ""), content_type=str(f.get("content_type") or ""),
            at=round(f["started"] - t0, 3) if (t0 is not None and f.get("started")) else None,
            elapsed=f.get("elapsed"), size=f.get("size"), frame="" if _same_page(frame, page) else frame,
            data=f.get("resource_type") in ("xhr", "fetch"),
            body=(f.get("body") or "")[:_VIEW_BODY_MAX] or None if bodies else None,
            truncated=bool(f.get("truncated")) or len(f.get("body") or "") > _VIEW_BODY_MAX,
        ))
    by_type: dict[str, int] = {}
    for r in reqs:
        by_type[r.type or "other"] = by_type.get(r.type or "other", 0) + 1
    static_nodes, data_nodes = _attribute(core, reqs, facts)
    _match_content(core, reqs, facts)
    view = NetworkView(url=page, document_id=core.name or "", requests=reqs, by_type=by_type,
                       static_nodes=static_nodes, data_nodes=data_nodes)
    data = [r for r in reqs if r.data]
    feeding = sorted((r for r in data if r.matched), key=lambda r: -r.matched)
    view.summary = (
        f"{len(reqs)} requests ({', '.join(f'{n} {t}' for t, n in sorted(by_type.items(), key=lambda x: -x[1])[:5])})"
        + (f"; {len(data)} data requests" if data else "; no data requests")
        + ("".join(f"; {r.method} {r.url[:90]} fills {r.matched} nodes" for r in feeding[:3]))
        + (f"; {static_nodes} nodes were there before any data arrived" if static_nodes else "")
    )
    return view


def _same_page(frame: str, page: str) -> bool:
    """Whether a request's frame is the page itself (not an embedded frame)."""
    if not frame:
        return True
    a, b = urlparse(frame), urlparse(page)
    return (a.netloc, a.path) == (b.netloc, b.path)


def _attribute(core: "Document", reqs: "list[NetRequest]", facts: "list[dict[str, Any]]") -> "tuple[int, int]":
    """Fill each data request's ``phase`` and ``produced`` from the correlation. Returns the counts
    of nodes before any data request / tied to one."""
    from ...models import DOMUpdateEvent, NetworkEvent
    from .correlate import ContentCorrelator, Correlator, OrderingCorrelator
    from .html import tree

    timeline = [e for e in core._events if isinstance(e, NetworkEvent) and e.index is not None]
    stamps = list(getattr(core, "_stamps", []) or [])
    if not timeline or not stamps:
        return (sum(1 for s in stamps if not s.get("xhr")), 0)
    # the timeline's i-th request to a URL is the i-th data fact to that URL (both in the page's own order)
    by_url: dict[str, list[NetRequest]] = {}
    for r in sorted((r for r in reqs if r.data), key=lambda r: (r.at or 0.0)):
        by_url.setdefault(r.url, []).append(r)
    taken: dict[str, int] = {}
    phase_of: dict[int, NetRequest] = {}
    for e in timeline:
        url = str(e.url or (e.request.dispatch("url") if e.request is not None else ""))
        k = taken.get(url, 0)
        taken[url] = k + 1
        lst = by_url.get(url) or []
        if k < len(lst):
            lst[k].phase = e.index
            phase_of[int(e.index or 0)] = lst[k]
            body = lst[k].body
            if e.body is None and body:
                e.body = body.encode("utf-8", "replace")  # content matching reads it
    dom = [DOMUpdateEvent(node_id=str(s.get("node") or ""), detail={
        "xhr_index": s.get("xhr", 0), "action": s.get("action", 0), "t_s": s.get("t"), "text": s.get("text", "")})
        for s in stamps]
    correlator: Correlator = ContentCorrelator() if any(e.body for e in timeline) else OrderingCorrelator()
    corr = correlator.correlate(timeline, dom)
    root = tree(core)
    static_nodes = data_nodes = 0
    per: dict[int, list[tuple[Any, float]]] = {}
    for ph in corr.phases:
        if not ph.candidates:
            static_nodes += 1
            continue
        if len(ph.candidates) > _CANDIDATES_MAX:
            continue
        el = _by_key(root, ph.node_key)
        if el is None:
            continue
        data_nodes += 1
        conf = ph.confidence if ph.confidence is not None else round(1 / len(ph.candidates), 2)
        for c in ph.candidates:
            per.setdefault(c, []).append((el, conf))
    for idx, nodes in per.items():
        req = phase_of.get(idx)
        if req is None:
            continue
        mine = {id(el) for el, _ in nodes}
        top = [(el, c) for el, c in nodes if not _has_ancestor_in(el, mine)]  # the outermost only
        req.produced = [NetNode(selector=_css_path(el), text=norm(" ".join(el.itertext()))[:160], confidence=c)
                      for el, c in top[:_NODES_MAX]]
    return static_nodes, data_nodes


def _match_content(core: "Document", reqs: "list[NetRequest]", facts: "list[dict[str, Any]]") -> None:
    """Tie the page's nodes to the data responses whose VALUES they show: every text-bearing element
    whose (normalised) text equals a string leaf of a JSON body is that field, rendered. Stronger
    than timing (a page that fires dozens of requests at once is ambiguous by time, never by value),
    and it names the field. A value in several responses splits the confidence. Content matches
    come first in ``produced``; timing-only ones follow."""
    from ...dom import parse_json
    from .html import tree

    root = tree(core)
    if root is None:
        return
    # the page's text: element -> its text, the DEEPEST element carrying a given text (a <li><a>T</a></li>
    # is the <a>), elements whose text is short enough to be a value
    by_text: dict[str, list[Any]] = {}
    for el in root.iter():
        if not isinstance(getattr(el, "tag", None), str) or el.tag.lower() in _SKIP:
            continue
        t = norm(" ".join(el.itertext()))
        if not (_VALUE_MIN <= len(t) <= 400):
            continue
        kids = [c for c in el if isinstance(getattr(c, "tag", None), str)]
        if len(kids) == 1 and norm(" ".join(kids[0].itertext())) == t:
            continue  # its only child says the same: the child is the node
        by_text.setdefault(t.lower(), []).append(el)
    if not by_text:
        return
    data = [(r, facts_body) for r in reqs if r.data for facts_body in [_body_of(r, facts)] if facts_body]
    owners: dict[str, list[tuple[NetRequest, str]]] = {}  # value -> the (request, field) it came from
    for r, body in data:
        parsed = parse_json(body)
        if parsed is None:
            continue
        paths: dict[str, list[str]] = {}
        for path, val in _leaves(parsed):
            v = norm(str(val)).lower()
            if len(v) < _VALUE_MIN or v not in by_text or (v.replace(".", "").isdigit() and len(v) < 6):
                continue
            paths.setdefault(v, []).append(path)
        for v, ps in paths.items():
            # a value at several places (every record's location) is the FIELD, not one record's
            owners.setdefault(v, []).append((r, ps[0] if len(ps) == 1 else _generalise(ps)))
    per: dict[int, list[NetNode]] = {}
    for v, srcs in owners.items():
        conf = round(1 / len(srcs), 2)
        for r, path in srcs:
            for el in by_text[v][:8]:
                per.setdefault(r.n, []).append(NetNode(selector=_css_path(el), text=norm(" ".join(el.itertext()))[:160], field=path, confidence=conf))
    for r in reqs:
        got = per.get(r.n)
        if not got:
            continue
        uniq: dict[str, NetNode] = {}
        for node in sorted(got, key=lambda x: -(x.confidence or 0)):
            uniq.setdefault(node.selector, node)
        mine = list(uniq.values())
        r.matched = len(mine)
        r.produced = (mine + [n for n in r.produced if n.selector not in uniq])[:_NODES_MAX]


def _generalise(paths: "list[str]") -> str:
    """The field several paths share (``jobs[0].title`` + ``jobs[7].title`` -> ``jobs[*].title``)."""
    shapes = {re.sub(r"\[\d+\]", "[*]", p) for p in paths}
    return shapes.pop() if len(shapes) == 1 else paths[0]


def _body_of(r: "NetRequest", facts: "list[dict[str, Any]]") -> str:
    """A data request's full captured body (the view may carry only its head)."""
    f = facts[r.n - 1] if 0 < r.n <= len(facts) else {}
    return str(f.get("body") or "")


def _leaves(data: Any, path: str = "", out: "list[tuple[str, Any]] | None" = None) -> "list[tuple[str, Any]]":
    """Every scalar leaf of a JSON value with its path (``jobs[3].title``), bounded."""
    out = [] if out is None else out
    if len(out) >= _LEAVES_MAX:
        return out
    if isinstance(data, dict):
        for k, v in data.items():
            _leaves(v, f"{path}.{k}" if path else str(k), out)
    elif isinstance(data, list):
        for i, v in enumerate(data):
            _leaves(v, f"{path}[{i}]", out)
    elif isinstance(data, (str, int, float)) and not isinstance(data, bool):
        out.append((path, data))
    return out


def _by_key(root: Any, key: str) -> Any:
    if root is None or not key:
        return None
    found = root.xpath(f'//*[@data-wc-node="{key}"]')
    return found[0] if found else None


def _has_ancestor_in(el: Any, ids: "set[int]") -> bool:
    p = el.getparent()
    while p is not None:
        if id(p) in ids:
            return True
        p = p.getparent()
    return False


def _css_path(el: Any) -> str:
    """A CSS path from the root to ``el`` (``html > body > main:nth-of-type(1) > …``): it picks this
    very node in the page's snapshot, which is what a view highlights."""
    steps: list[str] = []
    node = el
    while node is not None and isinstance(getattr(node, "tag", None), str):
        parent = node.getparent()
        tag = node.tag.lower()
        if parent is None:
            steps.append(tag)
            break
        same = [c for c in parent if isinstance(getattr(c, "tag", None), str) and c.tag == node.tag]
        steps.append(f"{tag}:nth-of-type({same.index(node) + 1})" if len(same) > 1 else tag)
        node = parent
    return " > ".join(reversed(steps))


__all__ = ["NetNode", "NetRequest", "NetworkView", "network_view"]
