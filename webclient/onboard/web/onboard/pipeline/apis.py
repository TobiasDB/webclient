"""Finding the DATA API behind a page -- deterministic, never a guessed URL: the DOM-declared /
linked JSON endpoints, and (after a browser render) the XHR / fetch stream the page fired. An
endpoint counts only when it is CONSISTENT with the page (its values appear in the page text)."""

from __future__ import annotations

import re
from urllib.parse import urlparse

from web.fetch import NetworkEvent, Snapshot
from web.parse import Document, parse

from .brief import Brief

_STOP = frozenset(
    "the a an of to for and or in on at by with its if is as from that this each per".split()
)


def words(*texts: str) -> "set[str]":
    out: set[str] = set()
    for t in texts:
        t = re.sub(r"([a-z])([A-Z])", r"\1 \2", t)
        out.update(w for w in re.findall(r"[a-z0-9]+", t.lower()) if len(w) > 2 and w not in _STOP)
    return out


def _keys(value: object, out: "set[str]", depth: int = 0) -> None:
    if depth > 6:
        return
    if isinstance(value, dict):
        for k, v in value.items():
            out.add(str(k))
            _keys(v, out, depth + 1)
    elif isinstance(value, list):
        for v in value[:5]:
            _keys(v, out, depth + 1)


def schema_fit(api: Document, brief: Brief) -> int:
    """How many of the brief's field words the API's key names cover."""
    keys: set[str] = set()
    _keys(api.json(), keys)
    want = words(*brief.names, *(f.description for f in brief.fields))
    return len(want & words(*keys))


def consistent(page: Document, api: Document) -> bool:
    """Whether ``api`` BACKS ``page``: enough of its distinctive leaves appear in the page text."""
    if api.kind != "json":
        return False
    leaves = [v for v in api.json_leaves(budget=2000) if len(v) >= 3][:40]
    if not leaves:
        return False
    text = page.text.lower()
    return sum(1 for v in leaves if v.lower() in text) >= max(2, len(leaves) // 4)


def records_path(value: object, path: str = "") -> str:
    """The dotted path to the FIRST list of objects in a JSON value (the record array), ``""`` when
    the value is itself that list or holds none."""
    if isinstance(value, list):
        return path if value and isinstance(value[0], dict) else ""
    if isinstance(value, dict):
        for k, v in value.items():
            sub = f"{path}.{k}" if path else str(k)
            if isinstance(v, list) and v and isinstance(v[0], dict):
                return sub
        for k, v in value.items():
            sub = f"{path}.{k}" if path else str(k)
            found = records_path(v, sub)
            if found:
                return found
    return ""


def has_records(value: object) -> bool:
    """Whether a JSON value holds a NON-EMPTY array of objects somewhere (the record array) -- a
    feed that answers with an empty list / a status envelope is not the dataset's feed."""
    if isinstance(value, list):
        return bool(value) and isinstance(value[0], dict)
    if isinstance(value, dict):
        return any(has_records(v) for v in value.values())
    return False


def declared_endpoints(doc: Document) -> "list[str]":
    """Same-origin JSON endpoints the DOM points at: feed / JSON ``<link>``s, ``/api/`` and
    ``.json`` links -- most declared first, deduped."""
    host = urlparse(doc.url).hostname
    out: list[str] = []
    declared = list(doc.metadata().feeds)
    for el in doc.select_all("link[rel=alternate][type*=json], link[type*=json]"):
        href = el.attr("href")
        if href:
            declared.append(href)
    linky = [
        u for u in doc.links() if "/api/" in u.lower() or u.lower().split("?")[0].endswith(".json")
    ]
    for u in [*declared, *linky]:
        if urlparse(u).hostname == host and u not in out:
            out.append(u)
    return out


class Feed:
    """A JSON call the rendered page made: its url, the parsed reply, and the REQUEST side
    (method, body, content type) -- what replaying it needs."""

    def __init__(
        self, url: str, doc: Document, method: str, body: bytes, content_type: str
    ) -> None:
        self.url, self.doc, self.method = url, doc, method or "GET"
        self.body, self.content_type = body, content_type


def observed_endpoints(snap: Snapshot) -> "list[Feed]":
    """The same-origin XHR / fetch calls in a rendered snapshot whose reply is JSON."""
    host = urlparse(snap.request.url).hostname
    out: list[Feed] = []
    seen: set[str] = set()
    for ev in snap.events:
        if not isinstance(ev, NetworkEvent) or ev.resource_type not in ("xhr", "fetch"):
            continue
        if not ev.body or ev.url in seen or urlparse(ev.url).hostname != host:
            continue
        seen.add(ev.url)
        doc = parse(ev.body, url=ev.url)
        if doc.kind == "json":
            out.append(Feed(ev.url, doc, ev.method, ev.request_body, ev.request_content_type))
    return out


__all__ = [
    "Feed",
    "consistent",
    "declared_endpoints",
    "has_records",
    "observed_endpoints",
    "records_path",
    "schema_fit",
    "words",
]
