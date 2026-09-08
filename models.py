"""Declarative web-client interface.

Layered design:

    Reference     -- immutable-ish description of *how to get* a resource
    Document      -- a fetched resource (httpx / bare transport), plus typed
                     views (html / json / xml / binary) and CSS selection
    LiveDocument  -- a browser-backed document (Playwright page pool);
                     stateful, records actions / XHR / DOM mutations
    Node          -- a selected element inside a Document (not a Document!)
    LiveNode      -- a selected element inside a LiveDocument (auto-waiting)
    WebClient     -- lifecycle root & service facade: owns the ClientPool,
                     EventBus, Sessions and the document / plan registries;
                     References, Documents and LiveDocuments are bound to
                     one (an implicit default is used when none is given)
    Session       -- logical identity (cookies, headers, proxy, browser
                     storage state) spanning many fetches
    ClientPool    -- bounded lease pool of concrete clients: httpx clients
                     and playwright pages
    EventBus      -- one shared pub/sub; correlation-tagged events routed
                     onto documents, user handlers, and the websocket API
    Expr / Lazy   -- Polars-style lazy expression DSL for declarative
                     extraction pipelines over the above
    Executor      -- compiles plans into dependency graphs and schedules
                     them against the pool in the right order

Everything here is interface: bodies are stubs (``...``) pending the engine
implementation on top of httpx + playwright.
"""
# mypy: disable-error-code="empty-body"

from __future__ import annotations

from enum import Enum
from typing import (
    Any,
    Callable,
    Iterable,
    Iterator,
    Literal,
    Sequence,
    TypeVar,
    overload,
)
from urllib.parse import parse_qs, urlparse

from pydantic import BaseModel, ConfigDict
from typing_extensions import Self  # typing.Self requires 3.11+

# --------------------------------------------------------------------------- #
# Common aliases / enums
# --------------------------------------------------------------------------- #

HttpMethod = Literal["get", "post", "put", "patch", "delete", "head", "options"]
WaitEvent = Literal["load", "domcontentloaded", "networkidle"]
ElementState = Literal["attached", "visible", "hidden", "detached"]

DEFAULT_PORTS: dict[str, int] = {"http": 80, "https": 443}


class OnError(str, Enum):
    """What an expression step does when its input is missing or it fails."""

    skip = "skip"            # drop the current row / element
    raise_error = "raise"    # abort the whole pipeline
    ignore = "ignore"        # keep the row, yield None for this field


class Proxy(BaseModel):
    url: str
    username: str | None = None
    password: str | None = None


class FetchError(Exception):
    """A fetch failed: transport error, or non-2xx status. Raised unless the
    fetch was made with ``optional=True``.

    Error philosophy, everywhere in this interface: **loud by default,
    leniency opt-in via ``optional=True``** -- a missing element, missing
    attribute, or failed fetch raises unless ``optional=True``, in which
    case selection/attr return None and fetch returns the (not-ok) Document
    for the caller to inspect via ``.ok``."""


class Script(BaseModel):
    """JS injected into a browser page."""

    source: str
    run_at: Literal["init", "domcontentloaded", "load"] = "init"


# Events. Everything observable flows over one shared EventBus, tagged with
# correlation ids so consumers can route them -- e.g. the WebClient assigns
# NetworkEvents to the LiveDocument whose page produced them. Recorded event
# lists on documents are just routed views of this bus.
#
# Topics are dotted strings matched by prefix (subscribing to "network" also
# receives "network.xhr"). The core taxonomy below covers what can happen on
# the web; plugin events SUBCLASS one of these (or Event itself for truly
# novel ones) and may introduce namespaced topics ("rrweb.dom.update").

Topic = str


class Event(BaseModel):
    topic: Topic
    source: str = "core"             # name of the emitting plugin
    seq: int | None = None           # per-document monotonic sequence number;
                                     # lets stream consumers detect gaps and
                                     # resume (WS /events?after=seq)
    ts: float | None = None
    # correlation ids -- set by the publisher, used for routing/filtering
    session_id: str | None = None
    document_id: str | None = None
    plan_id: str | None = None
    node_id: str | None = None       # element-level correlation: stable node
                                     # identity stamped by capture plugins
                                     # (rrweb node ids); enables LiveNode
                                     # event narrowing


E = TypeVar("E", bound=Event)


# core taxonomy: network
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


# core taxonomy: dom
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
    """Full-DOM checkpoint. Emitted periodically (by the dom/rrweb plugin)
    so stream consumers rebuilding content resync from the latest snapshot
    instead of replaying -- and diverging from -- a full incremental
    history. ``digest`` lets a client verify its rebuilt state."""

    topic: Topic = "dom.snapshot"
    snapshot: dict[str, Any] = {}    # serialized DOM (rrweb snapshot format)
    digest: str = ""                 # content hash of the DOM at capture


# core taxonomy: interaction & console
class ActionEvent(Event):
    topic: Topic = "action"
    action: str                      # "click", "write", "scroll", ...
    args: dict[str, Any] = {}


class ConsoleEvent(Event):
    topic: Topic = "console"
    level: Literal["log", "info", "warning", "error"]
    text: str


class Subscription(BaseModel):
    id: str
    topic: Topic

    def cancel(self) -> None: ...


class EventBus(BaseModel):
    """Shared pub/sub for the whole WebClient.

    Publishers: plugins attached to surfaces (all capture -- network, dom,
    console, action -- is pluginized), plus the pool and executor (pool /
    plan topics). Consumers: the WebClient's routing (appending events onto
    the owning document), user handlers, and the websocket API, which
    streams a filtered view of this bus to remote clients. Handlers must
    not block.
    """

    def publish(self, event: Event) -> None: ...

    def subscribe(self, topic: Topic, handler: Callable[[Event], None], *,
                  session_id: str | None = None,
                  document_id: str | None = None,
                  plan_id: str | None = None) -> Subscription:
        """Handler fires for events whose topic matches ``topic`` by dotted
        prefix and which match every given correlation filter."""
        ...


class EventRegistry(BaseModel):
    """Topic -> event class mapping, used to round-trip typed events over
    the wire (websocket API, stored plans) and by the lazy layer to resolve
    event types referenced in plans. Core events are pre-registered;
    ``WebClient.use`` registers each plugin's event types. An unknown topic
    resolves to its nearest registered ancestor ("rrweb.dom.update" ->
    DOMUpdateEvent), falling back to Event."""

    def register(self, cls: type[Event]) -> None: ...
    def resolve(self, topic: Topic) -> type[Event]: ...


# --------------------------------------------------------------------------- #
# Surfaces & Plugins
# --------------------------------------------------------------------------- #
# A Surface is an attachment point: an instance of something observable (a
# browser page, the http transport, a parsed document, a running plan). A
# Plugin declares which surface kinds it instruments and emits events into
# the shared bus through the surface handle. Built-in capture ships as core
# plugins on this same pathway -- replacing naive dom capture with RRWeb, or
# wrapping the browser for richer network capture, is just registration.

SurfaceKind = Literal["client", "session", "transport", "page", "document",
                      "node", "plan"]


class Surface(BaseModel):
    """Handle a plugin receives on attach. ``raw`` is the underlying object
    for the kind -- playwright Page ("page"), httpx client ("transport"),
    Document ("document"), ExecutionGraph ("plan"). ``emit`` publishes to
    the bus pre-tagged with this surface's correlation ids and the plugin's
    name, so plugin events route to the owning document for free."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    kind: SurfaceKind
    raw: Any = None
    session_id: str | None = None
    document_id: str | None = None
    plan_id: str | None = None

    def emit(self, event: Event) -> None: ...


class Plugin(BaseModel):
    """Base class for instrumentation plugins.

    Lifecycle: ``WebClient.use(plugin)`` registers it (attach order ==
    registration order) and registers its ``events`` with the client's
    EventRegistry. The engine calls ``attach`` whenever a surface instance
    of a kind in ``surfaces`` is created (for pages: after ``scripts`` are
    installed, before navigation) and ``detach`` when it is destroyed or
    the WebClient closes.

    Example -- RRWeb: ``surfaces=["page"]``, ``scripts=[rrweb source]``,
    ``events=[RRWebSnapshotEvent, ...]`` (subclasses of DOMEvent); attach
    exposes a page binding and translates rrweb messages into emits.

    The lazy interface works with plugins through the registry: an
    ``events_of`` op recorded in a chain names its event type by topic, and
    compile resolves it via EventRegistry -- so plans referencing plugin
    events (de)serialize and validate cleanly.
    """

    name: str
    version: str = "0"
    surfaces: list[SurfaceKind] = []
    events: list[type[Event]] = []   # event classes this plugin emits
    scripts: list[Script] = []       # injected into "page" surfaces pre-attach

    def attach(self, surface: Surface) -> None: ...
    def detach(self, surface: Surface) -> None: ...


class Element(BaseModel):
    """A typed content block -- the "elements" representation (the shape
    Unstructured popularized): what LLM consumers want instead of HTML."""

    id: str = ""
    type: str = "text"               # title/text/list_item/table/link/image/code
    text: str = ""
    parent_id: str | None = None
    metadata: dict[str, Any] = {}


class Renderer(Plugin):
    """A plugin providing named representations of documents, per document
    kind -- the mechanism behind ``Document.render`` and the service's
    ``GET /documents/{id}/render``. Representations are computed
    server-side from the parsed document; only the result crosses the
    network, never the full HTML (unless explicitly rendered as "html").

    Registered via ``WebClient.use`` like any plugin (``surfaces`` is
    implicitly ["document"]). Core renderers ship by default:

        html   -> markdown, text (readable), elements, links, html
        json   -> elements
        xml    -> text, elements
        binary -> (none; bytes are fetched via save / media endpoints)
    """

    kind: Literal["html", "json", "xml", "binary"] = "html"
    formats: list[str] = []

    def render(self, document: Document, format: str,
               **options: Any) -> Any: ...


# --------------------------------------------------------------------------- #
# Lifecycle: Session / ClientPool / WebClient
# --------------------------------------------------------------------------- #

LeaseKind = Literal["http", "page"]


class Lease(BaseModel):
    """A held transport resource: one httpx client or one browser page.
    One lease == one unit of parallelism."""

    id: str
    kind: LeaseKind
    session_id: str | None = None

    def release(self) -> None: ...


class PoolStats(BaseModel):
    http_total: int = 0
    http_free: int = 0
    pages_total: int = 0
    pages_free: int = 0
    waiting: int = 0


class ClientPool(BaseModel):
    """Bounded pool the WebClient leases concrete clients from. ``acquire``
    queues when the pool is exhausted (bounded by ``acquire_timeout``)."""

    max_http: int = 10
    max_pages: int = 4
    acquire_timeout: float = 60.0

    def acquire(self, kind: LeaseKind, *, session: Session | None = None,
                timeout: float | None = None) -> Lease: ...
    def release(self, lease: Lease) -> None: ...
    def stats(self) -> PoolStats: ...


class Session(BaseModel):
    """Logical identity spanning many fetches: cookies, headers, proxy and
    browser storage state. Created via ``WebClient.session()`` and owned by
    that WebClient. Leases acquired for it carry its id; its live pages
    share one browser context, so logins and storage persist across
    fetches."""

    id: str = ""
    status: Literal["pending", "running", "expired", "closed"] = "pending"
    ttl: float | None = None         # seconds of life; None -> client default
    keep_alive: bool = False         # survive client disconnects until ttl
    expires_at: float | None = None  # set by the owning WebClient
    headers: dict[str, str] = {}
    cookies: dict[str, str] = {}
    proxy: Proxy | None = None
    storage_state: dict[str, Any] | None = None   # browser cookies/localStorage
    timeout: float | None = None

    def ref(self, url: str, method: HttpMethod = "get",
            **kwargs: Any) -> Reference:
        """Build a Reference bound to this session (and its WebClient)."""
        ...

    def close(self) -> None:
        """Release this session's leases and persist ``storage_state``.
        Closing the owning WebClient closes every session."""
        ...


class WebClient(BaseModel):
    """Lifecycle root and service facade ("browser as a service").

    Owns the ClientPool, the EventBus, all Sessions, and registries (by id)
    of the Documents / LiveDocuments / query plans it produced. References,
    Documents and LiveDocuments are *bound* to the WebClient that made
    them: their fetch / reload / actions resolve through it, and closing it
    reclaims every lease, page and session.

    Event wiring: on fetch, the WebClient subscribes routing handlers for
    the new document's id, so network / dom / console / action events land
    in that document's recorded lists.

    The remote API is a thin adapter over exactly this surface -- an HTTP /
    websocket client drives a server-side WebClient by id:

        POST /sessions               -> session()  (ttl / keep_alive policy)
        GET|DELETE /sessions/{id}    -> status / close()
        WS   /sessions/{id}/cdp      -> raw CDP passthrough onto the
                                        session's browser context
        POST /fetch                  -> fetch()  (returns document id +
                                        metadata; content only on request)
        GET  /documents/{id}         -> document()  (metadata)
        GET  /documents/{id}/render  -> render(format)  (markdown / text /
                                        elements / links / html)
        POST /documents/{id}/select  -> server-side selection -> values
        POST /documents/{id}/actions -> replay ActionEvents on a live page
        POST /plans                  -> execute()   (submit a QueryPlan)
        GET  /plans/{id}/rows        -> results (paged or streamed rows)
        WS   /events?after=seq       -> bus.subscribe() (resumable stream)
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    # policy
    timeout: float = 30.0
    retries: int = 0
    verify_tls: bool = True
    default_headers: dict[str, str] = {}
    proxy_pool: list[Proxy] = []      # rotated for sessions without a proxy
    headless: bool = True
    default_scripts: list[Script] = []

    # owned infrastructure
    pool: ClientPool = ClientPool()
    bus: EventBus = EventBus()
    registry: EventRegistry = EventRegistry()
    plugins: list[Plugin] = []       # core capture plugins are pre-registered

    def use(self, plugin: Plugin) -> Self:
        """Register a plugin and its event types. Attach order follows
        registration order; core plugins come first unless replaced."""
        ...

    def __enter__(self) -> Self: ...
    def __exit__(self, *exc: object) -> None: ...
    def close(self) -> None:
        """Close all sessions, release all leases, stop the browser."""
        ...

    # -- sessions -----------------------------------------------------------
    def session(self, **overrides: Any) -> Session: ...

    # -- references / fetching ----------------------------------------------
    def ref(self, url: str, method: HttpMethod = "get",
            **kwargs: Any) -> Reference:
        """``Reference.from_url``, bound to this WebClient."""
        ...

    @overload
    def fetch(self, ref: Reference, *, session: Session | None = None,
              optional: bool = False) -> Document: ...
    @overload
    def fetch(self, ref: Reference, *, browser: Literal[True],
              session: Session | None = None,
              scripts: Sequence[Script] | None = None,
              wait_until: WaitEvent = "load",
              optional: bool = False) -> LiveDocument: ...
    def fetch(self, ref: Reference, *, browser: bool = False,
              session: Session | None = None,
              scripts: Sequence[Script] | None = None,
              wait_until: WaitEvent = "load",
              optional: bool = False) -> Document | LiveDocument: ...

    # -- registries (the service's handles) ---------------------------------
    def document(self, document_id: str) -> Document | None: ...

    def release(self, doc: LiveDocument) -> None:
        """Return a live page's lease to the pool (also happens on session
        close and WebClient close)."""
        ...

    # -- query execution ----------------------------------------------------
    def execute(self, plan: Expr | QueryPlan,
                context: Reference | Document | LiveDocument, *,
                stream: bool = False) -> Any:
        """Compile and run a plan via this client's Executor."""
        ...


def default_client() -> WebClient:
    """The lazily-created process-wide WebClient, used when a Reference is
    unbound and no ``client=`` is passed."""
    ...


# --------------------------------------------------------------------------- #
# Reference
# --------------------------------------------------------------------------- #

class Reference(BaseModel):
    """A request spec: everything needed to (re)fetch a resource."""

    hostname: str
    method: HttpMethod = "get"
    scheme: str = "https"
    port: int | None = None          # None -> default port for scheme
    path: str = ""
    fragment: str = ""

    params: dict[str, str | list[str]] = {}
    headers: dict[str, str] = {}
    cookies: dict[str, str] = {}
    body: bytes | None = None        # raw request body
    json_body: Any | None = None     # serialized as JSON, sets content-type
    form: dict[str, str] | None = None
    follow_redirects: bool = True
    timeout: float | None = None     # per-request override of Client.timeout

    @classmethod
    def from_url(
        cls,
        url: str,
        method: HttpMethod = "get",
        params: dict[str, str | list[str]] | None = None,
        headers: dict[str, str] | None = None,
        cookies: dict[str, str] | None = None,
    ) -> Self:
        parsed = urlparse(url)
        query: dict[str, str | list[str]] = {
            k: v[0] if len(v) == 1 else v for k, v in parse_qs(parsed.query).items()
        }
        if params:
            query.update(params)
        return cls(
            hostname=parsed.hostname or "",
            method=method,
            scheme=parsed.scheme or "https",
            port=parsed.port,            # None keeps scheme default
            path=parsed.path or "",
            fragment=parsed.fragment or "",
            params=query,
            headers=headers or {},
            cookies=cookies or {},
        )

    @property
    def url(self) -> str:
        """Reassembled absolute URL (default ports elided)."""
        ...

    # -- derivation helpers (used heavily by paginate) ----------------------
    def replace(self, **fields: Any) -> Self:
        """Copy with the given fields replaced."""
        ...

    def with_params(self, **params: str) -> Self:
        """Copy with query params merged in."""
        ...

    def join(self, href: str) -> Reference:
        """Resolve a (possibly relative) href against this reference."""
        ...

    # -- binding ------------------------------------------------------------
    def bind(self, client: WebClient, session: Session | None = None) -> Self:
        """Copy bound to a WebClient (and optionally a Session). Bound
        references resolve fetch/reload through that client; unbound ones
        fall back to ``default_client()``."""
        ...

    @property
    def bound(self) -> WebClient | None: ...

    # -- fetching -----------------------------------------------------------
    # A transport failure or non-2xx status raises FetchError unless
    # optional=True, which returns the Document regardless (check .ok).
    @overload
    def fetch(self, *, optional: bool = False,
              client: WebClient | None = None) -> Document: ...
    @overload
    def fetch(self, *, browser: Literal[True],
              scripts: Sequence[Script] | None = None,
              wait_until: WaitEvent = "load",
              optional: bool = False,
              client: WebClient | None = None) -> LiveDocument: ...
    def fetch(self, *, browser: bool = False,
              scripts: Sequence[Script] | None = None,
              wait_until: WaitEvent = "load",
              optional: bool = False,
              client: WebClient | None = None) -> Document | LiveDocument: ...


# --------------------------------------------------------------------------- #
# Nodes: selection results (an element is *not* a Document)
# --------------------------------------------------------------------------- #

class Node(BaseModel):
    """An element selected out of a Document.

    Carries a back-reference to its document so relative links resolve.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    @property
    def text(self) -> str:
        """Normalized text content of the element."""
        ...

    @property
    def html(self) -> str:
        """Outer HTML of the element."""
        ...

    @overload  # link-likes intentionally narrow str -> Reference
    def attr(self, name: Literal["href", "src", "action"]) -> Reference: ...  # type: ignore[overload-overlap]
    @overload
    def attr(  # type: ignore[overload-overlap]
        self, name: Literal["href", "src", "action"], *,
        optional: Literal[True]) -> Reference | None: ...
    @overload
    def attr(self, name: str) -> str: ...
    @overload
    def attr(self, name: str, *, optional: Literal[True]) -> str | None: ...
    def attr(self, name: str, *,
             optional: bool = False) -> Reference | str | None:
        """Attribute value; link-like attributes resolve to a Reference
        against the owning document's URL. A missing attribute raises
        LookupError unless ``optional=True`` (then None)."""
        ...

    # index selects the nth match; negative indexes from the end (-1 = last)
    @overload
    def select(self, selector: str, *, index: int = 0) -> Node: ...
    @overload
    def select(self, selector: str, *, index: int = 0,
               optional: Literal[True]) -> Node | None: ...
    def select(self, selector: str, *, index: int = 0,
               optional: bool = False) -> Node | None:
        """Select within this element by CSS or XPath (auto-detected:
        selectors starting with "/" or "./" are XPath). Selection returns
        elements only -- an XPath producing attributes or text (".../@href",
        "text()") is rejected with ValueError; use ``.attr()`` / ``.text``.
        Raises if no match unless ``optional=True``."""
        ...

    def select_all(self, selector: str, limit: int | None = None,
                   offset: int = 0) -> Sequence[Node]: ...

    @overload
    def events_of(self, event: type[E]) -> Sequence[E]: ...
    @overload
    def events_of(self, event: Topic) -> Sequence[Event]: ...
    def events_of(self, event: type[Event] | Topic) -> Sequence[Event]:
        """Events routed to the owning document. On a static Node this is
        document scope, unfiltered -- element narrowing requires the node
        identity that only live capture stamps (see LiveNode.events_of)."""
        ...


class LiveNode(Node):
    """An element inside a LiveDocument, backed by a Playwright locator.

    All interactions auto-wait for the element (visible + stable) up to the
    client timeout; pass ``timeout=`` to override, ``optional=True`` to make
    a missing element a no-op instead of an error.
    """

    def click(self, *, button: Literal["left", "middle", "right"] = "left",
              count: int = 1, timeout: float | None = None,
              optional: bool = False) -> Self: ...
    def write(self, text: str, *, clear: bool = True,
              delay_ms: int | None = None,
              timeout: float | None = None) -> Self: ...
    def hover(self, *, timeout: float | None = None) -> Self: ...
    def scroll_into_view(self) -> Self: ...
    def screenshot(self) -> BinaryDocument: ...

    # selection within a live element stays live
    @overload
    def select(self, selector: str, *, index: int = 0) -> LiveNode: ...
    @overload
    def select(self, selector: str, *, index: int = 0,
               optional: Literal[True]) -> LiveNode | None: ...
    def select(self, selector: str, *, index: int = 0,
               optional: bool = False) -> LiveNode | None: ...

    def select_all(self, selector: str, limit: int | None = None,
                   offset: int = 0) -> Sequence[LiveNode]: ...

    # element-scoped events, via capture-stamped node identity
    @overload
    def events_of(self, event: type[E]) -> Sequence[E]: ...
    @overload
    def events_of(self, event: Topic) -> Sequence[Event]: ...
    def events_of(self, event: type[Event] | Topic) -> Sequence[Event]:
        """Narrowed to this element: an event matches when its ``node_id``
        resolves to this node or a descendant (ancestor-path prefix over
        the capture plugin's stable node ids)."""
        ...


# --------------------------------------------------------------------------- #
# Documents
# --------------------------------------------------------------------------- #

# Pagination driver types. ``on`` decides how the next Reference is built:
#   * str          -- CSS selector; matched element's href/value becomes next
#   * Iterable     -- fixed iterable (dates, ints, cursors); each item is
#                     merged into the Reference via with_params/replace
#   * Callable     -- given the current page, returns the next Reference
#                     (or None to stop)
# ``until`` stops pagination; evaluated on the *next* page:
#   * str          -- CSS selector; pagination stops when it matches
#   * Callable     -- predicate on the fetched page
NextPage = str | Iterable[Any] | Callable[["Document"], "Reference | None"]
StopWhen = str | Callable[["Document"], bool]


class Document(Reference):
    """A fetched resource. Extends Reference so it can be re-fetched."""

    id: str = ""                     # registry handle in the owning WebClient
    session_id: str | None = None    # session that produced this document
    kind: Literal["html", "json", "xml", "binary"] = "html"
    content: bytes = b""
    status_code: int = 0
    response_headers: dict[str, str] = {}
    encoding: str | None = None      # detected / declared charset
    final_url: str | None = None     # after redirects
    elapsed: float | None = None     # seconds

    @property
    def text(self) -> str:
        """Body decoded using the detected encoding (utf-8 fallback)."""
        ...

    @property
    def ok(self) -> bool:
        """True for 2xx status codes."""
        ...

    # -- event store (filled by bus routing; see Surfaces & Plugins) --------
    events: list[Event] = []         # routed to this document's id; capped

    @overload
    def events_of(self, event: type[E]) -> Sequence[E]: ...
    @overload
    def events_of(self, event: Topic) -> Sequence[Event]: ...
    def events_of(self, event: type[Event] | Topic) -> Sequence[Event]:
        """Events routed to this document, filtered by event class (subclass
        match) or dotted topic prefix -- plugin events included."""
        ...

    @property
    def actions(self) -> Sequence[ActionEvent]:
        """View: ``events_of(ActionEvent)``."""
        ...

    def reload(self, *, optional: bool = False,
               client: WebClient | None = None) -> Self:
        """Re-fetch this document with its own request spec. Raises
        FetchError on failure unless ``optional=True``."""
        ...

    # -- representations (plugin-backed; see Renderer) ----------------------
    @overload
    def render(self, format: Literal["markdown", "text"], *,
               main_content_only: bool = False) -> str: ...
    @overload
    def render(self, format: Literal["elements"]) -> list[Element]: ...
    @overload
    def render(self, format: Literal["links"]) -> list[Reference]: ...
    @overload
    def render(self, format: str, **options: Any) -> Any: ...
    def render(self, format: str, **options: Any) -> Any:
        """Produce a named representation of this document, resolved to the
        Renderer registered for (kind, format) on the bound WebClient."""
        ...

    # -- typed views --------------------------------------------------------
    @property
    def html(self) -> HTMLDocument: ...
    @property  # shadows pydantic's deprecated v1-compat .json() on purpose
    def json(self) -> JSONDocument: ...  # type: ignore[override]
    @property
    def xml(self) -> XMLDocument: ...
    @property
    def binary(self) -> BinaryDocument: ...

    # -- selection (kind-appropriate parser; css or xpath, elements only;
    #    selecting on a kind with no tree, e.g. json, raises a typed error) --
    @overload
    def select(self, selector: str, *, index: int = 0) -> Node: ...
    @overload
    def select(self, selector: str, *, index: int = 0,
               optional: Literal[True]) -> Node | None: ...
    def select(self, selector: str, *, index: int = 0,
               optional: bool = False) -> Node | None: ...

    def select_all(self, selector: str, limit: int | None = None,
                   offset: int = 0) -> Sequence[Node]: ...

    # -- pagination ---------------------------------------------------------
    def paginate(
        self,
        on: NextPage,
        *,
        until: StopWhen | None = None,
        limit: int | None = None,
        offset: int = 0,
        resume: Reference | None = None,
        prefetch: int = 1,
        client: WebClient | None = None,
    ) -> Iterator[Document]:
        """Iterate pages starting from this one.

        ``on`` builds the next Reference (selector / iterable / callable, see
        NextPage). ``until`` stops iteration, evaluated on the next page.
        ``limit`` caps pages fetched; ``offset`` skips pages before yielding.
        ``resume`` restarts from the Reference of the last page a previous
        run yielded.

        Static pagination is concurrent: up to ``prefetch`` next pages are
        fetched while the current page is being processed.
        """
        ...


class HTMLDocument(Document):
    kind: Literal["html", "json", "xml", "binary"] = "html"

    @property
    def title(self) -> str | None: ...
    def links(self, selector: str = "a[href]") -> list[Reference]: ...

    # sugar over the plugin-backed representations
    @property
    def markdown(self) -> str:
        """View: ``render("markdown")``."""
        ...

    @property
    def elements(self) -> list[Element]:
        """View: ``render("elements")``."""
        ...


class JSONDocument(Document):
    kind: Literal["html", "json", "xml", "binary"] = "json"

    @property
    def data(self) -> Any:
        """Parsed JSON body."""
        ...

    def query(self, path: str) -> Any:
        """JMESPath-style query into the parsed body."""
        ...


class XMLDocument(Document):
    kind: Literal["html", "json", "xml", "binary"] = "xml"
    # no separate xpath method: select()/select_all() accept XPath on any
    # document (element-producing expressions only), namespaces included


class BinaryDocument(Document):
    kind: Literal["html", "json", "xml", "binary"] = "binary"
    media_type: str | None = None    # from content-type

    def save(self, path: str) -> str:
        """Write bytes to disk, returns the path written."""
        ...


# --------------------------------------------------------------------------- #
# LiveDocument: browser-backed, stateful
# --------------------------------------------------------------------------- #

LiveAction = Callable[["LiveDocument"], "LiveDocument | None"]


class LiveDocument(Document):
    """A Playwright-backed page.

    * Stateful: interactions mutate the underlying page; ``select`` results
      (LiveNode) observe the live DOM. Use ``reload()`` to reset state.
    * Every interaction auto-waits for its target (up to the client timeout)
      and raises on missing elements unless ``optional=True``.
    * ``actions`` / ``xhr_requests`` / ``dom_mutations`` record the session
      so it can be replayed onto a fresh page with ``replay``.
    * Events are published on the WebClient's shared bus and routed here by
      document id; ``subscribe`` is sugar for a bus subscription so scoped.
    * ``navigate`` returns a *new* LiveDocument reusing the same underlying
      page (session, cookies and storage persist).
    * Lifecycle is owned by the WebClient: the page lease is held until
      ``WebClient.release(self)``, the owning session closes, or the
      WebClient closes.
    """

    # typed views over the routed event store (Document.events / events_of)
    @property
    def xhr_requests(self) -> Sequence[XHREvent]: ...
    @property
    def dom_mutations(self) -> Sequence[DOMUpdateEvent]: ...
    @property
    def console(self) -> Sequence[ConsoleEvent]: ...

    # -- interactions (all auto-wait; chainable) ----------------------------
    def click(self, selector: str, *,
              button: Literal["left", "middle", "right"] = "left",
              count: int = 1, timeout: float | None = None,
              optional: bool = False) -> Self: ...
    def write(self, selector: str, text: str, *, clear: bool = True,
              delay_ms: int | None = None, timeout: float | None = None,
              optional: bool = False) -> Self: ...
    def press(self, key: str, *, selector: str | None = None,
              timeout: float | None = None) -> Self: ...
    def hover(self, selector: str, *, timeout: float | None = None,
              optional: bool = False) -> Self: ...
    def check(self, selector: str, checked: bool = True, *,
              timeout: float | None = None) -> Self: ...
    def select_option(self, selector: str, *,
                      value: str | None = None,
                      label: str | None = None,
                      index: int | None = None) -> Self: ...
    def upload(self, selector: str, files: Sequence[str]) -> Self: ...
    def drag(self, source: str, target: str) -> Self: ...

    @overload
    def scroll(self, selector: str, *, x: int = 0, y: int = 0) -> Self: ...
    @overload
    def scroll(self, *, x: int = 0, y: int = 0) -> Self: ...
    def scroll(self, selector: str | None = None, *,
               x: int = 0, y: int = 0) -> Self: ...

    # -- scripting ----------------------------------------------------------
    def execute(self, script: str | Script) -> Self:
        """Run JS for its side effects; chainable."""
        ...

    def evaluate(self, script: str) -> Any:
        """Run JS and return its (JSON-serializable) result."""
        ...

    def screenshot(self, selector: str | None = None, *,
                   full_page: bool = False,
                   format: Literal["png", "jpeg"] = "png") -> BinaryDocument: ...

    # -- waiting ------------------------------------------------------------
    @overload
    def wait_for(self, selector: str, *, state: ElementState = "visible",
                 timeout: float | None = None,
                 optional: bool = False) -> Self: ...
    @overload
    def wait_for(self, *, event: WaitEvent,
                 timeout: float | None = None) -> Self: ...
    @overload
    def wait_for(self, *, timeout: float) -> Self: ...
    def wait_for(self, selector: str | None = None, *,
                 event: WaitEvent | None = None,
                 state: ElementState = "visible",
                 timeout: float | None = None,
                 optional: bool = False) -> Self: ...

    # -- navigation ---------------------------------------------------------
    def navigate(self, target: Reference | str, *,
                 headers: dict[str, str] | None = None,
                 wait_until: WaitEvent = "load") -> LiveDocument: ...
    def back(self) -> LiveDocument: ...
    def forward(self) -> LiveDocument: ...

    # -- selection: returns live, auto-waiting nodes ------------------------
    @overload
    def select(self, selector: str, *, index: int = 0) -> LiveNode: ...
    @overload
    def select(self, selector: str, *, index: int = 0,
               optional: Literal[True]) -> LiveNode | None: ...
    def select(self, selector: str, *, index: int = 0,
               optional: bool = False) -> LiveNode | None: ...

    def select_all(self, selector: str, limit: int | None = None,
                   offset: int = 0) -> Sequence[LiveNode]: ...

    # -- session recording / events -----------------------------------------
    def replay(self, actions: Sequence[ActionEvent]) -> Self:
        """Re-apply recorded actions onto this page."""
        ...

    def subscribe(self, topic: Topic,
                  handler: Callable[[Event], None]) -> Subscription:
        """Sugar for ``bus.subscribe(topic, handler, document_id=self.id)``."""
        ...

    # -- pagination ---------------------------------------------------------
    def paginate(  # type: ignore[override]
        self,
        on: NextPage | LiveAction,
        *,
        until: StopWhen | LiveAction | None = None,
        limit: int | None = None,
        offset: int = 0,
        resume: Reference | None = None,
    ) -> Iterator[LiveDocument]:
        """Like Document.paginate, but ``on``/``until`` may also be browser
        actions (e.g. click "next", infinite scroll). Sequential -- the next
        page is produced only after the current one is fully processed, and
        action-driven iterators are not resumable.
        """
        ...


# --------------------------------------------------------------------------- #
# Lazy expression DSL (Polars-style, but for scraping)
# --------------------------------------------------------------------------- #
# The lazy layer is a thin recording wrapper over the *same* interface as the
# eager classes above: every attribute access or call on a lazy value returns
# another Expr recording that op -- REF.fetch() is already an Expr, and
# REF.fetch().select_all(".card") is just the next op in the plan. Nothing
# here pretends to be the eager types; rich per-method typing comes from
# outside the runtime instead:
#   * REPL / IPython -- __dir__ delegates to the wrapped class, so completion
#     on a lazy chain shows the eager interface at runtime;
#   * static checking -- generated stubs (a .pyi derived from the eager
#     classes, every method returning Expr) once the interface settles.
# A recorded pipeline (Expr) serializes to a JSON query plan, which is the
# wire format for the eventual HTTP / websocket API: clients build plans
# locally (or write the query syntax directly) and submit them for
# server-side execution.

class QueryPlan(BaseModel):
    """Serialized form of an Expr: the query-syntax / wire representation."""

    version: int = 1
    steps: list[dict[str, Any]] = []


class Expr:
    """A recorded chain of operations, executed by ``collect``.

    Any Reference/Document/LiveDocument/Node method can be called on an Expr;
    the call is recorded, not executed. Comparison operators build boolean
    expressions for use in ``filter``.
    """

    def __getattr__(self, name: str) -> Expr:
        """Record an attribute access (e.g. ``.text``) as the next op."""
        ...

    def __call__(self, *args: Any, **kwargs: Any) -> Expr:
        """Record a call of the preceding attribute (e.g. ``.select(...)``)."""
        ...

    def __dir__(self) -> list[str]:
        """Delegates to the interface of the expected value at this point in
        the chain, so REPL completion shows the eager API."""
        ...

    # -- query syntax / transport -------------------------------------------
    def to_query(self) -> QueryPlan:
        """Serialize the recorded plan (JSON-safe) for storage or transport
        over the HTTP / websocket API."""
        ...

    @classmethod
    def from_query(cls, plan: QueryPlan | dict[str, Any]) -> Expr:
        """Rebuild an executable Expr from a serialized plan (server side)."""
        ...

    # comparisons / logic -> boolean Expr
    def __eq__(self, other: Any) -> Expr: ...   # type: ignore[override]
    def __ne__(self, other: Any) -> Expr: ...   # type: ignore[override]
    def __and__(self, other: Expr) -> Expr: ...
    def __or__(self, other: Expr) -> Expr: ...
    def __invert__(self) -> Expr: ...

    def map(self, **fields: Expr | Any) -> Expr:
        """For each element of a list result, evaluate the given field
        expressions (each sees the element as its DOC context)."""
        ...

    def filter(self, predicate: Expr) -> Expr: ...

    def then(self, **fields: Expr | Any) -> Expr:
        """Add derived fields; expressions may reference earlier fields via
        ``col`` and are evaluated in declaration order."""
        ...

    def otherwise(self, state: OnError) -> Expr:
        """Error policy for the preceding step (skip / raise / ignore)."""
        ...

    def explain(self) -> str:
        """Human-readable plan of the recorded pipeline."""
        ...

    def collect(self, context: Reference | Document | LiveDocument, *,
                stream: bool = False, client: WebClient | None = None) -> Any:
        """Execute the pipeline against ``context``. Returns a JSON-like
        structure (list/dict of plain values); with ``stream=True`` returns
        an iterator yielding rows as they are produced."""
        ...


def col(name: str) -> Expr:
    """Reference a field produced earlier in the pipeline."""
    ...


def lit(value: Any) -> Expr:
    """Wrap a literal value as an Expr (passed through unchanged at collect
    time; useful where a field must be an Expr rather than a plain value)."""
    ...


class Lazy:
    """Entry point of a lazy chain: wraps an eager class so attribute access
    starts recording -- ``Lazy(Reference).fetch()`` is an Expr, and every
    further op returns another Expr. The wrapped class is remembered as the
    root context type of the plan (what ``collect`` must be given, and what
    ``__dir__`` completion is derived from)."""

    def __init__(self, cls: type) -> None: ...
    def __getattr__(self, name: str) -> Expr: ...
    def __dir__(self) -> list[str]: ...


class q:
    """The lazy namespace -- single entry point for building plans, polars
    style (``pl.col`` / ``pl.element``). When this becomes a package the
    same names are re-exported at module level, so ``import webclient as wc;
    wc.ref.fetch()`` reads identically over the HTTP / websocket client.
    """

    ref = Lazy(Reference)        # root context: a Reference to fetch
    doc = Lazy(Document)         # root context: an already-fetched Document
    live = Lazy(LiveDocument)    # root context: a browser-backed document
    node = Lazy(Node)            # element context inside map()
    live_node = Lazy(LiveNode)   # live element context

    col = staticmethod(col)
    lit = staticmethod(lit)


# --------------------------------------------------------------------------- #
# Query execution
# --------------------------------------------------------------------------- #
# Turning a plan into work, in the right order:
#   1. compile: QueryPlan -> ExecutionGraph. Each recorded op becomes a step
#      with explicit data dependencies (chain order; col() references) and a
#      declared resource need: fetch() -> "http"; fetch(browser=True) and
#      live actions -> "page"; pure ops (select / attr / compare) -> none.
#   2. run: repeatedly start every step whose dependencies are met, gated by
#      pool.acquire for its resource. Pure steps run inline; http steps run
#      concurrently up to the pool bound; steps sharing a page (a live fetch
#      and the actions chained after it) hold one lease and serialize on it
#      in plan order. map() fans out one subgraph per element at run time,
#      bounded so memory stays flat.
#   3. rows stream out as their subgraphs complete; each step's OnError
#      policy decides skip / raise / ignore on failure.

Resource = Literal["http", "page"]


class ExecutionStep(BaseModel):
    id: str
    op: dict[str, Any]               # one op from QueryPlan.steps
    depends_on: list[str] = []
    resource: Resource | None = None
    page_group: str | None = None    # steps sharing a page lease serialize
    on_error: OnError = OnError.raise_error


class ExecutionGraph(BaseModel):
    plan_id: str
    steps: list[ExecutionStep] = []


class RunStats(BaseModel):
    rows: int = 0
    requests: int = 0
    pages_used: int = 0
    errors: int = 0
    elapsed: float = 0.0
    done: bool = False


class Executor(BaseModel):
    """Compiles and schedules plans against its WebClient's pool. One
    executor per WebClient; remote plan submission (POST /plans) lands
    here, and ``status``/``cancel`` are the service's progress surface.
    Publishes "plan" events (started / row / error / done) on the bus."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    def compile(self, plan: QueryPlan) -> ExecutionGraph: ...

    @overload
    def run(self, graph: ExecutionGraph,
            context: Reference | Document | LiveDocument) -> Any: ...
    @overload
    def run(self, graph: ExecutionGraph,
            context: Reference | Document | LiveDocument, *,
            stream: Literal[True]) -> Iterator[dict[str, Any]]: ...
    def run(self, graph: ExecutionGraph,
            context: Reference | Document | LiveDocument, *,
            stream: bool = False) -> Any: ...

    def status(self, plan_id: str) -> RunStats: ...
    def cancel(self, plan_id: str) -> None: ...


# --------------------------------------------------------------------------- #
# Usage examples (not executed -- the engine is not implemented yet)
# --------------------------------------------------------------------------- #

def _example_imperative() -> None:
    with WebClient() as wc:
        doc = wc.ref("https://example.com/path?query=param").fetch()

        for card in doc.select_all(".card"):
            title = card.select(".title").text
            link = card.attr("href")              # Reference, bound to wc
            print(f"Title: {title}, Link: {link.url}")

            live = link.fetch(browser=True)       # leases a page from wc's pool
            live.click(".load-more", optional=True)
            live.wait_for(".content", timeout=5.0)
            print(live.select(".content").text)
            wc.release(live)                      # page back to the pool
    # leaving the block closes sessions, releases leases, stops the browser


def _example_lazy() -> None:
    # Every op on a lazy proxy is already an Expr -- q.ref.fetch() records a
    # fetch, .select_all() records the next op, and the combinators are
    # available anywhere in the chain. Completion comes from __dir__ in the
    # REPL; static stubs can be generated once the interface settles.
    expr = (
        q.ref.fetch().select_all(".card")
        .map(
            # map() runs per element of the list, so the context is a Node
            title=q.node.select(".title").text,
            is_active=q.node.select(".active").text == "Active",
            link=q.node.attr("href").otherwise(OnError.skip),
        )
        .filter(q.col("is_active"))
        .then(
            content=q.col("link").fetch(browser=True)
            .click(".load-more")
            .wait_for(".content", timeout=5.0)
            .select(".content").text,
            date=q.col("content").select(".date").text,
        )
        .otherwise(OnError.raise_error)
    )

    ref = Reference.from_url("https://example.com/cards")
    rows = expr.collect(ref)                      # json-like rows; stream=True to iterate
    print(rows)

    # The same plan, serialized -- this is what an HTTP / websocket client
    # would submit for server-side execution:
    plan = expr.to_query()
    print(plan.model_dump_json())
    server_side = Expr.from_query(plan)
    server_side.collect(ref)
