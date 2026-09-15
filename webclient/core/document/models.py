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
    #: how the bytes were obtained: the transport tiers this resolution took (the
    #: last is ``final_tier``), e.g. ``["static"]`` or ``["static", "proxy", "browser"]``.
    escalation: list[str] = ["static"]
    final_tier: str = "static"


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


class Signal(BaseModel):
    """One detected fact about a resolved response -- self-describing, so an LLM (or
    the auto-escalation loop) can act on it directly. A signal reads from whatever
    the document already carries: the access signals (``anti_bot`` / ``blocked`` /
    ``paywall`` / ``login_wall``) from the status/headers/cookies/body -- available
    on ANY fetch; the JS-nature signals (``body_injected`` / ``xhr_composed`` /
    ``client_shell``, rolled up by ``spa``) from the captured DOM/network events --
    filled on a browser render. Empty (``present=False``) is normal, not an error:
    it means "nothing notable of this kind."

    Several signals can point at the same conclusion from different evidence -- a
    page is a SPA because ``xhr_composed`` (its main content is correlated with
    same-origin XHR, rrweb-style) AND ``body_injected`` (most of the body text
    appeared after the initial response). Each is its own signal; read whichever you
    want, or the ``spa`` roll-up.

    The ``webclient.resiliency.detect`` layer computes the raw facts; the signals
    facet wraps each into this shape."""

    #: the detector's stable identifier, e.g. ``"anti_bot"`` / ``"body_injected"`` /
    #: ``"xhr_composed"`` -- so a signal from ``signals()`` is self-identifying.
    name: str = ""
    present: bool = False
    #: the evidence, e.g. ">40% of the page's text was injected after load via
    #: same-origin XHR" or "datadome challenge on a 403".
    reason: str = ""
    #: the salient metric or label behind the signal -- an ``injected_ratio`` (0.62),
    #: a vendor name ("datadome"), a status code (403). Type varies by signal.
    value: Any = None
    #: the transport escalation that would plausibly help -- ``"browser"`` (render
    #: JS), ``"proxy"`` (rotate IP past a block), ``"stealth"`` (a browser behind a
    #: proxy, for a named anti-bot vendor), or ``None`` (nothing the client can do,
    #: e.g. a paywall / login wall). The auto-resolve loop reads this to escalate.
    remedy: Literal["browser", "proxy", "stealth"] | None = None

    def __bool__(self) -> bool:
        return self.present


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
        def anti_bot(self) -> "Signal": ...
        @overload
        def attr(self, name: Literal['href', 'src', 'action']) -> "Reference": ...  # type: ignore[overload-overlap]
        @overload
        def attr(self, name: str, *, optional: bool = ..., error: Any = ...) -> "Field[str]": ...
        def blocked(self) -> "Signal": ...
        def body_injected(self) -> "Signal": ...
        def click(self, selector: str | None = ..., *, timeout: float | None = ..., optional: bool = ..., error: Any = ...) -> "Document": ...
        def client_shell(self) -> "Signal": ...
        def elements(self) -> "list[Element]": ...
        def evaluate(self, script: str) -> "Any": ...
        @overload
        def events_of(self, event_type: type[E]) -> "list[E]": ...
        @overload
        def events_of(self, event_type: str) -> "list[Event]": ...
        def framework(self) -> "str | None": ...
        def html(self) -> "str": ...
        def is_empty(self) -> "Field[bool]": ...
        def is_ok(self) -> "Field[bool]": ...
        def links(self) -> "Collection[Reference]": ...
        def login_wall(self) -> "Signal": ...
        def markdown(self, *, main_content_only: bool = ...) -> "str": ...
        def metadata(self) -> "Metadata": ...
        def paywall(self) -> "Signal": ...
        def ref(self) -> "Reference": ...
        def reload(self) -> "Document": ...
        @overload
        def render(self, format: Literal['elements']) -> "list[Element]": ...
        @overload
        def render(self, format: Literal['links']) -> "Collection[Reference]": ...
        @overload
        def render(self, format: str, **options: Any) -> "str": ...
        def screenshot(self, selector: str | None = ...) -> "Document": ...
        def select(self, selector: str, *, index: int = ..., optional: bool = ..., error: Any = ...) -> "Document": ...
        def select_all(self, selector: str, *, limit: int | None = ..., offset: int = ...) -> "Collection[Document]": ...
        def signals(self) -> "list[Signal]": ...
        def skeleton(self, *, max_lines: int = ..., text_chars: int = ..., max_depth: int = ..., max_siblings: int = ..., legend: bool = ..., collapse: bool = ..., annotate_origin: bool = ...) -> "str": ...
        def spa(self) -> "Signal": ...
        def structure(self) -> "Structure": ...
        def text(self, *, main_content_only: bool = ...) -> "str": ...
        def transport(self) -> "Transport": ...
        def wait_for(self, selector: str | None = ..., *, timeout: float | None = ..., optional: bool = ..., error: Any = ...) -> "Document": ...
        def write(self, selector: str, text: str, *, timeout: float | None = ..., optional: bool = ..., error: Any = ...) -> "Document": ...
        def xhr_composed(self) -> "Signal": ...
        def xhr_endpoints(self) -> "list[XhrCall]": ...
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
    "Signal",
]
