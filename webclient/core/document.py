"""DocumentCore: the machinery behind a Document (PLAN §5c).

Holds a resolved document's runtime -- the page lease, the live element
locator, the parsed html/xml tree or json value -- and the op logic:
which backing serves ``select``/``click``, how an element Document is
built, snapshot invalidation, capability computation. It interacts with the
WebClientCore (leases, navigation, release). The public ``Document`` keeps
the data and a thin ``@policy`` method surface that delegates here.
"""
from __future__ import annotations

import json as _json
from typing import Any

from .base import Capability, UnsupportedOperation


class DocumentCore:
    __slots__ = ("doc", "page", "lease", "routing", "attached",
                 "locator", "tree", "data", "is_element", "identity")

    def __init__(self, doc: Any) -> None:
        self.doc = doc                       # the public Document (data)
        self.page = None                     # playwright page (live root)
        self.lease = None
        self.routing = None
        self.attached: list[Any] = []
        self.locator = None                  # a live element's locator
        self.tree: Any = None                # cached lxml tree / element
        self.data: Any = None                # cached json (sub)value
        self.is_element = False
        self.identity: str | None = None     # live element dom path (cached)

    @property
    def client(self) -> Any:
        return self.doc._client

    # -- backing dispatch (PLAN §5b) -----------------------------------------
    def choose(self) -> list[Any]:
        from .backings import choose
        return choose(self)

    def backing(self, op: str) -> Any:
        for backing in self.choose():
            if op in backing.provides:
                return backing
        from .backings import GATES
        raise UnsupportedOperation(op, GATES.get(op, "ok"), self.capabilities())

    def dispatch(self, op: str, *args: Any, **kwargs: Any) -> Any:
        """Run ``op`` through its backing (which receives this core)."""
        return getattr(self.backing(op), op)(self, *args, **kwargs)

    def capabilities(self) -> frozenset[Capability]:
        caps: set[Capability] = {"ok"} if self.doc.ok else set()
        for backing in self.choose():
            caps.add(backing.gate)
        return frozenset(caps)

    # -- parsing / snapshots -------------------------------------------------
    def invalidate(self) -> None:
        """A mutating action drops the cached tree; the next read re-snapshots
        the live DOM (Decision 15)."""
        self.tree = self.data = None

    async def asnapshot(self) -> None:
        """Refresh cached content from the live page (async; the core does not
        bridge to the loop itself -- a live read awaits this first)."""
        if self.page is not None and not self.is_element:
            self.doc.content = (await self.page.content()).encode()
            self.tree = self.data = None

    def parsed(self) -> Any:
        if self.tree is None:
            from lxml import etree
            from lxml import html as _lxml_html
            kind = self.doc.kind
            if kind == "html":
                self.tree = _lxml_html.fromstring(self.doc.content or b"<html></html>")
            elif kind == "xml":
                if not self.doc.content:
                    raise ValueError("cannot parse an empty XML document")
                tree = etree.fromstring(
                    self.doc.content, parser=etree.XMLParser(recover=True))
                if tree is None:
                    raise ValueError("XML content is unparseable, even leniently")
                self.tree = tree
            else:
                raise TypeError(
                    "select() requires a parsed tree; "
                    f"this document kind is {kind!r}")
        return self.tree

    def text(self) -> str:
        """Decoded element/body text: element inner-text, json value, or the
        body decoded via declared encoding, else detection, else utf-8."""
        doc = self.doc
        if self.is_element:
            if doc.kind == "json":
                return self.data if isinstance(self.data, str) else _json.dumps(self.data)
            return " ".join("".join(self.parsed().itertext()).split())
        if not doc.content:
            return ""
        if doc.encoding:
            try:
                return doc.content.decode(doc.encoding)
            except (LookupError, UnicodeDecodeError):
                pass
        from charset_normalizer import from_bytes as _detect
        best = _detect(doc.content).best()
        return str(best) if best is not None else \
            doc.content.decode("utf-8", errors="replace")

    def json(self) -> Any:
        if self.data is None and not self.is_element:
            self.data = _json.loads(self.text())
        return self.data

    # -- element / binary construction ---------------------------------------
    def element(self, *, tree: Any = None, data: Any = None,
                locator: Any = None, content: bytes = b"",
                kind: str | None = None, identity: str | None = None) -> Any:
        """A selected element as a Document: same request/response identity,
        backing = the subtree / json value / live locator, root = this."""
        doc = self.doc
        if tree is not None and not content:
            from lxml import etree
            content = etree.tostring(tree, encoding="utf-8")
        elif data is not None and not content:
            content = _json.dumps(data).encode()
        sub = type(doc)(url=doc.url, final_url=doc.final_url,
                        kind=kind or doc.kind, status_code=doc.status_code,
                        encoding=doc.encoding, response_headers=doc.response_headers,
                        session_id=doc.session_id, content=content,
                        root=doc.name or None)
        sub._client, sub._session = doc._client, doc._session
        sub.events = doc.events              # share the store for narrowing
        core = sub._core
        core.tree, core.data, core.is_element = tree, data, True
        core.locator = locator
        core.identity = identity
        core.page = self.page if locator is not None else None
        return sub

    def binary(self, data: bytes, format: str) -> Any:
        doc = self.doc
        shot = type(doc)(url=doc.url, content=data, status_code=200,
                         kind="binary")
        shot._client = doc._client
        return shot

    # -- live element identity (event narrowing, ISSUES #9) ------------------
    def identity_path(self) -> str | None:
        """The live element's DOM path, captured when it was selected (async),
        so event narrowing needs no loop bridge here."""
        return self.identity
