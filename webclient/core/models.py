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
from typing import TYPE_CHECKING, Any, ClassVar, Literal, Sequence, cast, overload
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

from pydantic import BaseModel, PrivateAttr
from typing_extensions import Self

from .base import (CLASSES, RETURN, Capability, Collection,
                        ErrorPolicy, Field, OpError, UnsupportedOperation,
                        WebBase, WebError, policy)

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

class Reference(WebBase):
    """A request spec plus the action chain and resolve options that led to a
    document (Decision 11): everything needed to (re)resolve it.

    ``Reference("https://...")`` (positional) is a lazy root -- a plan that
    starts by resolving that URL (Decision 14). Keyword construction is an
    eager Reference."""

    kind: str = "webpage"
    hostname: str
    method: HttpMethod = "get"
    scheme: str = "https"
    port: int | None = None          # None -> default port for scheme
    path: str = ""
    fragment: str = ""

    params: dict[str, str | list[str]] = {}
    headers: dict[str, str] = {}
    cookies: dict[str, str] = {}
    body: bytes | None = None
    json_body: Any | None = None
    form: dict[str, str] | None = None
    follow_redirects: bool = True
    timeout: float | None = None
    actions: list[dict[str, Any]] = []   # ordered ops taken from the document
    options: dict[str, Any] = {}         # resolve options used (browser, ...)

    if TYPE_CHECKING:
        # positional-url construction is a lazy root (see __new__); tell the
        # checker Reference accepts it, typed as Reference via lazy(cls)->cls.
        def __init__(self, url: str | None = None, /, **data: Any) -> None: ...

    def __new__(cls, url: str | None = None, /, **data: Any) -> Reference:
        if url is not None and cls is Reference:
            from .expr import Plan, lazy
            spec = Reference.from_url(url).request_fields()
            return lazy(Reference, plan=Plan(root="Reference", source=spec))
        return super().__new__(cls)

    def request_fields(self) -> dict[str, Any]:
        """The request spec alone -- no WebBase identity/state fields."""
        return {n: getattr(self, n) for n in _REQUEST_FIELDS}

    @classmethod
    def from_url(cls, url: str, method: HttpMethod = "get",
                 params: dict[str, str | list[str]] | None = None,
                 headers: dict[str, str] | None = None,
                 cookies: dict[str, str] | None = None) -> Self:
        parsed = urlparse(url)
        query: dict[str, str | list[str]] = {
            k: v[0] if len(v) == 1 else v for k, v in parse_qs(parsed.query).items()}
        if params:
            query.update(params)
        return cls(hostname=parsed.hostname or "", method=method,
                   scheme=parsed.scheme or "https", port=parsed.port,
                   path=parsed.path or "", fragment=parsed.fragment or "",
                   params=query, headers=headers or {}, cookies=cookies or {})

    @property
    def url(self) -> str:
        """Reassembled absolute URL (default ports elided)."""
        port = ""
        if self.port is not None and self.port != DEFAULT_PORTS.get(self.scheme):
            port = f":{self.port}"
        url = f"{self.scheme}://{self.hostname}{port}{self.path}"
        if self.params:
            url += "?" + urlencode(self.params, doseq=True)
        if self.fragment:
            url += "#" + self.fragment
        return url

    # -- derivation ----------------------------------------------------------
    def replace(self, **fields: Any) -> Self:
        """A derived reference: unnamed, rooted at this one."""
        return self.model_copy(update={**fields, "name": "",
                                       "root": self.name or self.root})

    def with_params(self, **params: str) -> Self:
        return self.replace(params={**self.params, **params})

    def join(self, href: str) -> Reference:
        return Reference.from_url(urljoin(self.url, href))

    def bind(self, client: Any, session: Any = None) -> Self:
        copy = self.model_copy()
        copy._client = client
        copy._session = session
        return copy

    @property
    def bound(self) -> Any:
        return self._client

    # -- resolving -----------------------------------------------------------
    @policy(returns="Document", require="ok")
    def resolve(self, *, browser: bool = False, session: Any = None,
                optional: bool = False, error: ErrorPolicy | None = None,
                **options: Any) -> Document:
        """Resolve this reference into a Document via the bound client's core;
        an unbound reference records instead (a lazy plan). ``optional=True``
        returns a not-ok Document instead of raising on failure."""
        wc = self._client or (self._session._client if self._session else None)
        if wc is None:
            from .expr import Expr, Plan
            root = Expr(Plan(root="Reference", source=self.request_fields()))
            return root.resolve(browser=browser, optional=optional, **options)  # type: ignore[return-value]
        return wc.resolve(self, browser=browser, optional=optional,  # type: ignore[return-value]
                          session=session or self._session, **options)

_REQUEST_FIELDS = [n for n in Reference.model_fields
                   if n not in WebBase.model_fields]


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


class Document(WebBase):
    """A resolved resource -- or an element of one (``select`` returns
    Documents whose backing is a subtree, root = the parent). A sibling of
    Reference (spec.py): it holds the *response* and its own action chain,
    and ``ref()`` returns the Reference it resolves from."""

    id: str = ""
    session_id: str | None = None
    kind: Literal["html", "json", "xml", "binary"] = "html"
    url: str = ""                    # the resolved (request) URL
    final_url: str | None = None     # after redirects
    content: bytes = b""
    status_code: int = 0
    response_headers: dict[str, str] = {}
    encoding: str | None = None
    elapsed: float | None = None
    events: list[Event] = []
    actions: list[dict[str, Any]] = []   # ordered ops taken from this document
    options: dict[str, Any] = {}         # resolve options used

    _core_obj: Any = PrivateAttr(default=None)   # lazy DocumentCore (machinery)

    @property
    def _core(self) -> Any:
        """The DocumentCore holding this document's runtime and op logic."""
        if self._core_obj is None:
            from .document import DocumentCore
            self._core_obj = DocumentCore(self)
        return self._core_obj

    def _capabilities(self) -> frozenset[Capability]:
        return self._core.capabilities()


    def _is_empty(self) -> bool:
        return not self.content

    @property
    def text(self) -> str:
        """Decoded element/body text (delegates to the DocumentCore)."""
        return self._core.text()


    def join(self, href: str) -> Reference:
        """Resolve ``href`` against the document's final URL (after redirects),
        not the original request URL."""
        return Reference.from_url(urljoin(self.final_url or self.url, href))

    def ref(self) -> Reference:
        """The Reference this document resolves from, reflecting the current
        action chain (Decision 11): re-resolving it reproduces this state."""
        if self._client is not None and self.root:
            found = self._client.reference(self.root)
            if found is not None:
                found.actions = list(self.actions)   # current chain
                found.options = dict(self.options)
                return found
        ref = Reference.from_url(self.url)   # best-effort (registry lost it)
        ref.name, ref.actions, ref.options = self.root or "", list(self.actions), dict(self.options)
        return ref.bind(self._client, self._session)

    def reload(self, **options: Any) -> "Document":
        """Re-resolve this document's reference, replaying its recorded action
        chain (Decision 11) so the result reproduces this state."""
        return self.ref().resolve(**{**self.options, **options})

    # -- event store ---------------------------------------------------------
    @overload
    def events_of(self, event: type[E]) -> Sequence[E]: ...
    @overload
    def events_of(self, event: Topic) -> Sequence[Event]: ...
    def events_of(self, event: type[Event] | Topic) -> Sequence[Event]:
        if isinstance(event, str):
            matches = [e for e in self.events
                       if e.topic == event or e.topic.startswith(event + ".")]
        else:
            matches = [e for e in self.events if isinstance(e, event)]
        path = self.identity_path if self._core.locator is not None else None
        if path is None:
            return matches
        return [e for e in matches if e.node_id is not None
                and (e.node_id == path or e.node_id.startswith(path + "/"))]

    @property
    def action_events(self) -> Sequence[ActionEvent]:
        """Captured interactions; ``actions`` is the replayable chain."""
        return self.events_of(ActionEvent)

    # -- representations: one render() with per-format typing ----------------
    @overload
    def render(self, format: Literal["markdown", "text", "html"],
               **options: Any) -> str: ...
    @overload
    def render(self, format: Literal["elements"], **options: Any) -> list[Element]: ...
    @overload
    def render(self, format: Literal["links"], **options: Any) -> Collection[Reference]: ...
    @overload
    def render(self, format: str, **options: Any) -> Any: ...
    @policy(returns="None")
    def render(self, format: str, **options: Any) -> Any:
        """Render a representation of this document -- a backing op dispatched
        by kind (core: markdown / text / elements / links / html for html;
        elements for json). A client may override a (kind, format) via
        ``wc.use(Renderer)``; the backing consults that table first. On a live
        page the backing renders off a fresh snapshot (settled by @policy)."""
        return self._core.dispatch("render", format, **options)

    @property
    def title(self) -> str | None:
        node = self.select("title", error=RETURN)
        return node.text if node.ok else None

    # -- selection & actions: delegated to the DocumentCore (PLAN §5c) -------
    def _parsed(self) -> Any:
        return self._core.parsed()

    @policy(returns="Document")
    def select(self, selector: str, *, index: int = 0,
               wait: float | None = None, error: ErrorPolicy | None = None,
               optional: bool = False) -> Document:
        """CSS or XPath on html/xml, a dotted path on json, the live DOM on a
        page backing (Decision 15). Element selection nests."""
        return self._core.dispatch("select", selector, index=index, wait=wait)  # type: ignore[return-value]

    @policy(returns="Collection")
    def select_all(self, selector: str, limit: int | None = None,
                   offset: int = 0, *, error: ErrorPolicy | None = None
                   ) -> Collection[Document]:
        return self._core.dispatch("select_all", selector, limit=limit, offset=offset)  # type: ignore[return-value]

    @overload  # link-likes intentionally narrow to Reference
    def attr(self, name: Literal["href", "src", "action"], *,  # type: ignore[overload-overlap]
             error: ErrorPolicy | None = None) -> Reference: ...
    @overload
    def attr(self, name: str, *, error: ErrorPolicy | None = None) -> Field[str]: ...
    @policy(returns="Field")
    def attr(self, name: str) -> Field[str] | Reference:
        """An attribute; ``text``/``html`` read the element's text or outer
        html, a link attribute comes back as a Reference joined against this
        document."""
        return self._core.dispatch("attr", name)  # type: ignore[return-value]

    # -- interactions (live backing; @policy settles the coroutine) ----------
    @policy(returns="Self")
    def click(self, selector: str | None = None, *, error: ErrorPolicy | None = None,
              **kw: Any) -> Self:
        return self._core.dispatch("click", selector, **kw)  # type: ignore[return-value]

    @policy(returns="Self")
    def write(self, selector: str, text: str, *,
              error: ErrorPolicy | None = None, **kw: Any) -> Self:
        return self._core.dispatch("write", selector, text, **kw)  # type: ignore[return-value]

    @policy(returns="Self")
    def press(self, key: str, *, error: ErrorPolicy | None = None, **kw: Any) -> Self:
        return self._core.dispatch("press", key, **kw)  # type: ignore[return-value]

    @policy(returns="Self")
    def hover(self, selector: str, *, error: ErrorPolicy | None = None, **kw: Any) -> Self:
        return self._core.dispatch("hover", selector, **kw)  # type: ignore[return-value]

    @policy(returns="Self")
    def check(self, selector: str, checked: bool = True, *,
              error: ErrorPolicy | None = None, **kw: Any) -> Self:
        return self._core.dispatch("check", selector, checked, **kw)  # type: ignore[return-value]

    @policy(returns="Self")
    def select_option(self, selector: str, *,
                      error: ErrorPolicy | None = None, **kw: Any) -> Self:
        return self._core.dispatch("select_option", selector, **kw)  # type: ignore[return-value]

    @policy(returns="Self")
    def upload(self, selector: str, files: Sequence[str], *,
               error: ErrorPolicy | None = None) -> Self:
        return self._core.dispatch("upload", selector, files)  # type: ignore[return-value]

    @policy(returns="Self")
    def drag(self, source: str, target: str, *,
             error: ErrorPolicy | None = None) -> Self:
        return self._core.dispatch("drag", source, target)  # type: ignore[return-value]

    @policy(returns="Self")
    def scroll(self, selector: str | None = None, *, x: int = 0, y: int = 0,
               error: ErrorPolicy | None = None) -> Self:
        return self._core.dispatch("scroll", selector, x=x, y=y)  # type: ignore[return-value]

    @policy(returns="Self")
    def execute(self, script: str, *, error: ErrorPolicy | None = None) -> Self:
        return self._core.dispatch("execute", script)  # type: ignore[return-value]

    @policy(returns="Field")
    def evaluate(self, script: str, *, error: ErrorPolicy | None = None) -> Any:
        return self._core.dispatch("evaluate", script)

    @policy(returns="Document")
    def screenshot(self, selector: str | None = None, *,
                   error: ErrorPolicy | None = None, **kw: Any) -> Document:
        return self._core.dispatch("screenshot", selector, **kw)  # type: ignore[return-value]

    @policy(returns="Self")
    def wait_for(self, selector: str | None = None, *,
                 error: ErrorPolicy | None = None, **kw: Any) -> Self:
        return self._core.dispatch("wait_for", selector, **kw)  # type: ignore[return-value]

    # -- live event views + element-scoped narrowing (ISSUES #9) -------------
    @property
    def identity_path(self) -> str | None:
        """A live element's capture identity ("n1/n4"), for event narrowing."""
        return self._core.identity_path()

    @property
    def xhr_requests(self) -> Sequence[XHREvent]:
        return self.events_of(XHREvent)

    @property
    def dom_mutations(self) -> Sequence[DOMUpdateEvent]:
        return self.events_of(DOMUpdateEvent)

    @property
    def console(self) -> Sequence[ConsoleEvent]:
        return self.events_of(ConsoleEvent)

    def subscribe(self, topic: Topic, handler: Any) -> Any:
        return self._client.bus.subscribe(topic, handler, document_id=self.id)

# ``LiveDocument``/``LiveNode`` are now ``Document`` (a live backing / a live
# element); the typed views (HTMLDocument/JSONDocument/…) are gone -- the
# representation methods live on Document, keyed by ``kind``.
LiveDocument = Document
LiveNode = Document


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
