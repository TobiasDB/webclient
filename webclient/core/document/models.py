"""Document's model + interface.

``IDocument`` is a resolved resource's data: its Core Fields (the response) plus,
under ``TYPE_CHECKING``, the eager ops ``Document`` implements (generated from
the document backings). ``Document`` inherits it and adds only behaviour
(backings, dispatch, ``_sub``). The ops are ``TYPE_CHECKING``-only, so at runtime
this is just the data model and ``WebCore.__getattr__`` dispatches every op.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal, TypeVar, overload  # noqa: F401

from pydantic import BaseModel

from ...errors import WebError

if TYPE_CHECKING:
    # cores/collections the generated ops return (the value models they return
    # are defined below in this module).
    from ...collection import Collection, Field  # noqa: F401
    from ...models import ActionEvent, ConsoleEvent, DOMUpdateEvent, Event  # noqa: F401
    from ...surfaces.lazy import LazyDocument  # noqa: F401
    from ..reference import Reference  # noqa: F401
    from . import Document  # noqa: F401

    E = TypeVar("E", bound="Event")  # events_of(type[E]) -> list[E]


# --------------------------------------------------------------------------- #
# The value models the document backings produce (all pure pydantic).
# --------------------------------------------------------------------------- #


class Element(BaseModel):
    """A typed content block -- the "elements" representation of a document (the
    html/json selection backings produce these)."""

    id: str = ""
    type: str = "text"
    text: str = ""
    parent_id: str | None = None
    metadata: dict[str, Any] = {}


class TocEntry(BaseModel):
    level: int
    text: str


class Form(BaseModel):
    method: str = "get"
    action: str | None = None
    field_names: list[str] = []


class XhrCall(BaseModel):
    method: str
    url: str


class Transport(BaseModel):
    """Transport facts -- free from any resolved document (values only for the
    few that *are* the summary; headers/cookies are key lists)."""

    final_url: str
    status_code: int
    ok: bool
    kind: str
    redirect_chain: list[str] = []
    duration_ms: float | None = None
    content_type: str | None = None
    encoding: str | None = None
    size_bytes: int | None = None
    header_keys: list[str] = []
    set_cookie_keys: list[str] = []
    server: str | None = None
    cdn: str | None = None
    region: str | None = None


class Metadata(BaseModel):
    """Head / schema metadata -- title/description are values; og and JSON-LD are
    reported as key/type names."""

    title: str | None = None
    description: str | None = None
    lang: str | None = None
    canonical_url: str | None = None
    schema_types: list[str] = []
    og_keys: list[str] = []
    page_type: str | None = None
    feeds: list[str] = []
    sitemap_url: str | None = None


class Structure(BaseModel):
    """Body shape -- counts, a table of contents, forms (as field-name lists) and
    detected pagination."""

    toc: list[TocEntry] = []
    word_count: int | None = None
    reading_time_min: int | None = None
    main_content_present: bool | None = None
    links_internal: int = 0
    links_external: int = 0
    link_sample: list[str] = []
    forms: list[Form] = []
    pagination: str | None = None
    media_img: int = 0
    media_video: int = 0


class Runtime(BaseModel):
    """Browser-only signals, read from captured DOM/network events (``None`` on a
    static fetch)."""

    is_spa: bool | None = None
    framework: str | None = None
    uses_xhr: bool | None = None
    uses_fetch: bool | None = None
    xhr_endpoints: list[XhrCall] = []
    dynamic_elements: list[str] = []
    #: how the page was built (the finer detail behind ``is_spa``, so a caller can set
    #: its own threshold): the fraction of the page's text that was injected AFTER the
    #: initial load (0.0 = fully server-rendered, ~1.0 = a client-rendered shell),
    #: the count of nodes injected after load, and whether that injected content
    #: landed in the main content area (position: middle-of-page injection is a
    #: stronger SPA signal than an edge widget).
    injected_ratio: float | None = None
    injected_nodes: int | None = None
    injected_in_main: bool | None = None
    #: the page rewrote its MAIN content after navigation using data it fetched from
    #: its OWN origin -- i.e. the content is composed client-side from ``xhr_endpoints``.
    #: When true, an agent can often **skip rendering the page** and fetch those
    #: endpoints directly (they are the real data source).
    content_from_xhr: bool | None = None


class Probe(BaseModel):
    """What an auto-resolve had to escalate to (``None`` unless the resolution
    recorded it) -- the read-side of the resiliency layer."""

    was_browser_required: bool | None = None
    was_proxy_required: bool | None = None
    anti_bot: str | None = None
    js_required: bool | None = None
    paywall: bool | None = None
    login_wall: bool | None = None
    render_blocked: bool | None = None
    #: extra visible words a browser render recovered over the static response
    #: (only set by ``browser="probe"``): >0 means JS injects content worth a
    #: browser; 0 means the static HTML already carried it.
    render_gain: int | None = None


class ProbeRecord(BaseModel):
    """The raw resolution record the transport ladder writes onto a Document as it
    fetches / escalates -- the source the ``probe`` summary facet projects from (and
    the wire form for a remote resolve). Richer than the facet: it also keeps the
    tier trail. See :mod:`docs.design.resiliency`."""

    was_browser_required: bool = False
    was_proxy_required: bool = False
    anti_bot: str | None = None  # cloudflare / datadome / perimeterx / ... / None
    js_required: bool = False
    paywall: bool = False
    login_wall: bool = False
    render_blocked: bool = False
    render_gain: int | None = None  # probe: extra visible words the browser recovered
    escalation: list[str] = []  # tiers taken, e.g. ["static", "browser"]
    reason: str = ""  # the final trigger, e.g. "datadome-403"
    attempts: int = 1
    final_tier: str = "static"


class IDocument(BaseModel):
    """A resolved resource's data (the Core Fields), plus (for the checker) the
    eager ops ``Document`` implements -- ``select`` / ``attr`` / ``text_content``
    / ``render`` / the event views / the live interaction set. The ops are
    ``TYPE_CHECKING``-only, so at runtime this is just the data model."""

    # -- Core Fields (the resolved response) ---------------------------------
    id: str = ""
    name: str = ""  # scoped document name
    root: str = ""  # the originating reference's name
    session_id: str = ""  # owning session (if any)
    kind: Literal["html", "json", "xml", "binary"] = "html"
    url: str = ""
    final_url: str | None = None
    content: bytes = b""
    status_code: int = 0
    response_headers: dict[str, str] = {}
    encoding: str | None = None
    elapsed: float | None = None
    created: float = 0.0
    accessed: float = 0.0
    error: WebError | None = None

    if TYPE_CHECKING:
        # >>> generated: Document interface <<<
        # fmt: off
        @property
        def action_events(self) -> list[ActionEvent]: ...
        @property
        def console(self) -> list[ConsoleEvent]: ...
        @property
        def dom_mutations(self) -> list[DOMUpdateEvent]: ...
        @property
        def events(self) -> list[Event]: ...
        @property
        def message(self) -> str: ...
        @property
        def region(self) -> str: ...
        @property
        def text_content(self) -> str | None: ...
        @property
        def title(self) -> str | None: ...
        @overload
        def attr(self, name: Literal['href', 'src', 'action']) -> "Reference": ...  # type: ignore[overload-overlap]
        @overload
        def attr(self, name: str, *, optional: bool = ..., error: Any = ...) -> "Field[str]": ...
        def click(self, selector: str | None = ..., *, timeout: float | None = ..., optional: bool = ..., error: Any = ...) -> "Document": ...
        def elements(self) -> "list[Element]": ...
        def evaluate(self, script: str) -> "Any": ...
        @overload
        def events_of(self, event_type: type[E]) -> "list[E]": ...
        @overload
        def events_of(self, event_type: str) -> "list[Event]": ...
        def html(self) -> "str": ...
        def is_empty(self) -> "Field[bool]": ...
        def is_ok(self) -> "Field[bool]": ...
        def links(self) -> "Collection[Reference]": ...
        def markdown(self, *, main_content_only: bool = ...) -> "str": ...
        def metadata(self) -> "Metadata": ...
        def probe(self) -> "Probe": ...
        def ref(self) -> "Reference": ...
        def reload(self) -> "Document": ...
        @overload
        def render(self, format: Literal['elements']) -> "list[Element]": ...
        @overload
        def render(self, format: Literal['links']) -> "Collection[Reference]": ...
        @overload
        def render(self, format: str, **options: Any) -> "str": ...
        def runtime(self) -> "Runtime": ...
        def screenshot(self, selector: str | None = ...) -> "Document": ...
        def select(self, selector: str, *, index: int = ..., optional: bool = ..., error: Any = ...) -> "Document": ...
        def select_all(self, selector: str, *, limit: int | None = ..., offset: int = ...) -> "Collection[Document]": ...
        def skeleton(self, *, max_lines: int = ..., text_chars: int = ..., max_depth: int = ..., max_siblings: int = ..., legend: bool = ..., collapse: bool = ..., annotate_origin: bool = ...) -> "str": ...
        def structure(self) -> "Structure": ...
        def text(self, *, main_content_only: bool = ...) -> "str": ...
        def transport(self) -> "Transport": ...
        def wait_for(self, selector: str | None = ..., *, timeout: float | None = ..., optional: bool = ..., error: Any = ...) -> "Document": ...
        def write(self, selector: str, text: str, *, timeout: float | None = ..., optional: bool = ..., error: Any = ...) -> "Document": ...
        # fmt: on
        # >>> end generated <<<
        pass


__all__ = [
    "IDocument",
    "Element",
    "TocEntry",
    "Form",
    "XhrCall",
    "Transport",
    "Metadata",
    "Structure",
    "Runtime",
    "Probe",
    "ProbeRecord",
]
