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


class PageCard(BaseModel):
    """A lean page descriptor -- what ``doc.card()`` projects: enough to understand a
    page and rebuild a :class:`Reference` for it, without keeping the whole Document.
    The default crawl projection (``project=doc.card()``), so ``crawl.pages`` holds
    these unless a different projection expression is given."""

    url: str
    final_url: str | None = None
    kind: str = "html"  # the sniffed content type (html / json / xml / binary)
    status_code: int = 0
    title: str | None = None
    description: str | None = None  # metadata description, when present
    flags: list[str] = []  # the names of the flags that fired (spa / login / ...)
    final_tier: str = "static"  # how the bytes were obtained (traceability)
    escalation: list[str] = ["static"]


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


#: the stage a signal was observed at -- how much work it took to see it. ``request``
#: = the response status/headers/cookies; ``static`` = the served HTML; ``rendered``
#: = the post-render DOM (needs a browser); ``network`` = the page's XHR/fetch traffic
#: (needs a browser). Front-loaded detection fills the cheap stages first.
Stage = Literal["request", "static", "rendered", "network"]


class Signal(BaseModel):
    """One piece of EVIDENCE for a :class:`Flag` -- a single detector firing, tagged
    with the ``stage`` it was observed at and a ``confidence`` (0-1) for how strongly
    it indicates the flag. Several signals (often from different stages) corroborate
    one flag: a page is a SPA from a ``framework_marker`` (static) AND ``body_injected``
    (rendered) AND ``xhr_composed`` (network). The ``webclient.signals`` registry
    (request/static) and the ``flags`` facet (rendered/network) emit these."""

    name: str = ""  # detector id, e.g. "empty_root_shell" / "rel_next_link"
    flag: str = ""  # the flag it feeds, e.g. "spa" / "anti_bot_triggered"
    stage: Stage = "static"
    confidence: float = 0.0  # 0-1: how strongly this evidence indicates the flag
    contra: bool = False  # CONTRA evidence: reduces the flag's confidence instead of raising it
    reason: str = ""  # human/LLM-readable evidence
    value: Any = None  # the salient metric / endpoint / label

    def __bool__(self) -> bool:
        return self.confidence > 0.0


class Flag(BaseModel):
    """A CONCLUSION the caller (and the ``auto`` ladder) acts on -- SPA, anti-bot,
    login, pagination, forms, buttons -- built from its :class:`Signal` evidence.
    ``confidence`` is the noisy-OR of the signals' confidences (corroborating evidence
    raises it); ``present`` is that crossing a threshold. ``remedy`` is the transport
    escalation it calls for, when any. ``value`` carries the actionable payload (the
    XHR endpoints behind a SPA, the next-page pattern, the form/button list). A flag
    with no evidence is empty (``present=False``) -- normal, not an error."""

    name: str = ""
    present: bool = False
    confidence: float = 0.0
    signals: list[Signal] = []
    #: the escalation this flag calls for: ``"browser"`` (render JS), ``"proxy"``
    #: (rotate IP), ``"stealth"`` (a browser behind a proxy, for a named anti-bot),
    #: or ``None`` (nothing the transport can do -- e.g. a login wall).
    remedy: Literal["browser", "proxy", "stealth"] | None = None
    value: Any = None  # the actionable payload (endpoints / pattern / elements)

    def __bool__(self) -> bool:
        return self.present


class IDocument(BaseModel):
    """A resolved resource's data (the Core Fields), plus (for the checker) the
    eager ops ``Document`` implements -- ``select`` / ``attr`` (incl. ``attr("text")``)
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
        def title(self) -> str | None: ...
        def anti_bot_present(self) -> "Flag": ...
        def anti_bot_triggered(self) -> "Flag": ...
        def as_json(self) -> "Document": ...
        @overload
        def attr(self, name: Literal['href', 'src', 'action']) -> "Reference": ...  # type: ignore[overload-overlap]
        @overload
        def attr(self, name: str, pattern: str | None = ..., *, group: int | str | None = ..., optional: bool = ..., error: Any = ...) -> "str | None": ...
        def buttons(self) -> "Flag": ...
        def card(self) -> "PageCard": ...
        def click(self, selector: str | None = ..., *, timeout: float | None = ..., optional: bool = ..., error: Any = ...) -> "Document": ...
        def elements(self) -> "list[Element]": ...
        def evaluate(self, script: str, *, mutates: bool = ...) -> "Any": ...
        @overload
        def events_of(self, event_type: type[E]) -> "list[E]": ...
        @overload
        def events_of(self, event_type: str) -> "list[Event]": ...
        def flags(self) -> "list[Flag]": ...
        def forms(self) -> "Flag": ...
        def framework(self) -> "str | None": ...
        def html(self) -> "str": ...
        def iframe(self) -> "Flag": ...
        def is_empty(self) -> "bool | None": ...
        def is_ok(self) -> "bool | None": ...
        def large_document(self) -> "Flag": ...
        def links(self) -> "Collection[Reference]": ...
        def login_present(self) -> "Flag": ...
        def login_required(self) -> "Flag": ...
        def markdown(self, *, main_content_only: bool = ...) -> "str": ...
        def metadata(self) -> "Metadata": ...
        def pagination(self) -> "Flag": ...
        def ref(self) -> "Reference": ...
        def regex(self, pattern: str, *, group: int | str = ..., flags: str = ...) -> "str | None": ...
        def regex_all(self, pattern: str, *, group: int | str = ..., flags: str = ...) -> "list[str]": ...
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
        def shadow_dom(self) -> "Flag": ...
        def skeleton(self, *, max_lines: int = ..., text_chars: int = ..., max_depth: int = ..., max_siblings: int = ..., legend: bool = ..., collapse: bool = ..., drop_chrome: bool = ..., annotate_origin: bool = ..., correlate: bool = ..., mark_records: bool = ..., mark_interactive: bool = ...) -> "str": ...
        def spa(self) -> "Flag": ...
        def structure(self) -> "Structure": ...
        def text(self, *, main_content_only: bool = ...) -> "str": ...
        def transport(self) -> "Transport": ...
        def wait_for(self, selector: str | None = ..., *, timeout: float | None = ..., optional: bool = ..., error: Any = ...) -> "Document": ...
        def write(self, selector: str, text: str, *, timeout: float | None = ..., optional: bool = ..., error: Any = ...) -> "Document": ...
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
    "PageCard",
    "Metadata",
    "Structure",
    "Signal",
    "Flag",
]
