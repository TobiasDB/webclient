"""The surface objects (spec.py): WebBase, WebError, Field, Collection,
Reference, Document.

These are plain eager classes: a method does the real work and raises on
failure. The ``@policy`` decorator gives every method the
``error=IGNORE|RETURN|RAISE`` envelope and capability checks. Recording is
NOT here -- it lives in ``core.expr.Expr``, which is independent of these
classes; a lazy chain is an ``Expr``, never one of these. The evaluator
replays a plan by calling these same methods, so eager and lazy cannot
drift.
"""
from __future__ import annotations

import json as _json
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Literal, Sequence, overload
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

from pydantic import BaseModel, PrivateAttr

from .base import (CLASSES, Capability, Collection, ErrorPolicy, Field,
                   UnsupportedOperation, WebBase, WebError)

from ..events import (
    ActionEvent,
    AssetEvent,
    ConsoleEvent,
    DOMUpdateEvent,
    E,
    Event,
    FetchEvent,
    NavigationEvent,
    NetworkEvent,
    Topic,
    XHREvent,
)

HttpMethod = Literal["get", "post", "put", "patch", "delete", "head", "options"]

DEFAULT_PORTS: dict[str, int] = {"http": 80, "https": 443}



class FetchError(Exception):
    """A fetch failed: transport error, or non-2xx status. Raised unless the
    fetch was made with ``optional=True`` (which instead returns the not-ok
    Document for inspection via ``.ok``). Carries ``document`` when a
    response was received."""

    def __init__(self, message: str, document: "Document | None" = None):
        super().__init__(message)
        self.document = document


class Script(BaseModel):
    """JS injected into a browser page (M4)."""

    source: str
    run_at: Literal["init", "domcontentloaded", "load"] = "init"


class Element(BaseModel):
    """A typed content block -- the "elements" representation."""

    id: str = ""
    type: str = "text"               # title/text/list_item/table/code/image
    text: str = ""
    parent_id: str | None = None
    metadata: dict[str, Any] = {}


# --------------------------------------------------------------------------- #
# Selection engine
# --------------------------------------------------------------------------- #

def _is_xpath(selector: str) -> bool:
    return selector.startswith("/") or selector.startswith("./")


def _select_elements(root: Any, selector: str) -> list[Any]:
    """CSS or XPath (auto-detected) -> element list. Elements only: XPath
    producing attributes, text or scalars is rejected (ISSUES #10)."""
    from lxml import etree
    if _is_xpath(selector):
        results = root.xpath(selector)
        if not isinstance(results, list):  # count(), boolean(), string()
            raise ValueError(
                f"XPath {selector!r} produces a scalar; selection returns "
                "elements only -- use .attr() / .text for values")
        non_elements = [r for r in results if not isinstance(r, etree._Element)]
        if non_elements:
            raise ValueError(
                f"XPath {selector!r} selects non-element results; selection "
                "returns elements only -- use .attr() / .text for values")
        return results
    return root.cssselect(selector)


def _json_path(data: Any, path: str) -> Any:
    """Dotted-path query with [n] indexing, e.g. "items[0].name" (JMESPath-
    lite; a full engine is a later, deliberate dependency)."""
    import re
    value = data
    try:
        for token in re.findall(r"[^.\[\]]+|\[\d+\]", path):
            value = value[int(token[1:-1])] if token.startswith("[") else value[token]
    except (KeyError, IndexError, TypeError) as exc:
        raise LookupError(f"no match for {path!r} in json") from exc
    return value


# --------------------------------------------------------------------------- #
# WebBase / WebError
# --------------------------------------------------------------------------- #

# --------------------------------------------------------------------------- #
# Reference
# --------------------------------------------------------------------------- #


_REQUEST_FIELDS = [n for n in Reference.model_fields
                   if n not in WebBase.model_fields]


# -- Reference construction / derivation (off the data class, PLAN §9) ------- #

def from_url(url: str, method: HttpMethod = "get",
             params: dict[str, str | list[str]] | None = None,
             headers: dict[str, str] | None = None,
             cookies: dict[str, str] | None = None) -> "Reference":
    """Build a Reference from a URL string."""
    parsed = urlparse(url)
    query: dict[str, str | list[str]] = {
        k: v[0] if len(v) == 1 else v for k, v in parse_qs(parsed.query).items()}
    if params:
        query.update(params)
    return Reference(hostname=parsed.hostname or "", method=method,
                     scheme=parsed.scheme or "https", port=parsed.port,
                     path=parsed.path or "", fragment=parsed.fragment or "",
                     params=query, headers=headers or {}, cookies=cookies or {})


def request_fields(ref: "Reference") -> dict[str, Any]:
    """The request spec alone -- no WebBase identity/state fields."""
    return {n: getattr(ref, n) for n in _REQUEST_FIELDS}


def bind(ref: "Reference", client: Any, session: Any = None) -> "Reference":
    """A copy of ``ref`` bound to a client (and session)."""
    copy = ref.model_copy()
    copy._client = client
    copy._session = session
    return copy


def url_of(ref: "Reference") -> str:
    """Reassembled absolute URL (default ports elided) -- the ``url`` prop."""
    port = ""
    if ref.port is not None and ref.port != DEFAULT_PORTS.get(ref.scheme):
        port = f":{ref.port}"
    url = f"{ref.scheme}://{ref.hostname}{port}{ref.path}"
    if ref.params:
        url += "?" + urlencode(ref.params, doseq=True)
    if ref.fragment:
        url += "#" + ref.fragment
    return url


# --------------------------------------------------------------------------- #
# Document
# --------------------------------------------------------------------------- #

def apply_status(doc: "Document") -> "Document":
    """Set ok/error/message from a document's HTTP status (the resolver calls
    this at build time -- the model does not derive it itself)."""
    if not 200 <= doc.status_code < 300:
        doc.ok = False
        doc.error = WebError(
            type="HTTPStatus" if doc.status_code else "NoResponse",
            message=(f"{doc.url} -> {doc.status_code}" if doc.status_code
                     else f"{doc.url}: no response"))
        doc.message = doc.error.message
    return doc


# NetworkEvent.request is a forward ref to Reference (events.py must not
# import models at runtime, ISSUES #13); resolve it now.
for _cls in (NetworkEvent, XHREvent, FetchEvent, NavigationEvent, AssetEvent):
    _cls.model_rebuild(_types_namespace={"Reference": Reference})

# Register the surface classes for the policy decorator's not-ok results, and
# install the typed lazy roots (doc / many / ref).
CLASSES.update(WebBase=WebBase, WebError=WebError, Field=Field,
               Collection=Collection, Reference=Reference, Document=Document)
from .expr import _install_roots as _install_roots  # noqa: E402
_install_roots()


# ========================================================================= #
# DocumentCore: the per-document runtime + op machinery (merged from
# core/document.py). Lives with the data models it drives (PLAN §9).
# ========================================================================= #

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

    # -- reference / reload (moved off Document, PLAN §9) --------------------
    def join(self, href: str) -> "Reference":
        """Resolve ``href`` against the document's final URL (after redirects)."""
        doc = self.doc
        return from_url(urljoin(doc.final_url or doc.url, href))

    def ref(self) -> "Reference":
        """The Reference this document resolves from, reflecting the current
        action chain (Decision 11) so re-resolving reproduces this state."""
        doc = self.doc
        if doc._client is not None and doc.root:
            found = doc._client.reference(doc.root)
            if found is not None:
                found.actions = list(doc.actions)
                found.options = dict(doc.options)
                return found
        ref = from_url(doc.url)        # best-effort (registry lost it)
        ref.name, ref.actions, ref.options = (
            doc.root or "", list(doc.actions), dict(doc.options))
        return bind(ref, doc._client, doc._session)

    def reload(self, **options: Any) -> Any:
        """Re-resolve this document's reference, replaying its action chain."""
        from .ops import run_op
        return run_op(self.ref(), "resolve", [], {**self.doc.options, **options})

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
        sub._core_obj = core = DocumentCore(sub)   # bind the element's core
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
