"""The Summary schema: a compact, deterministic, token-lean overview of a
resolved Document.

A summary is a *shape*, not a data dump -- headers, cookies and page metadata are
reported as KEYS / TYPES, not values (the values themselves stay reachable via
``attr()`` / ``response_headers``). It is assembled from independent facet
backings, each an optional section: ``transport`` (transport facts),
``metadata`` (head/schema), ``structure`` (body shape), ``runtime`` (browser
signals -- DOM/network events) and ``probe`` (what an auto-resolve had to
escalate to). A section is ``None`` when its facet was not requested or does not
apply to how the document was resolved. Populated by
``webclient.core.document.summary``.
"""

from __future__ import annotations

from pydantic import BaseModel

#: the facet sections, in order; the selector for ``summary(include=...)``.
FACETS = ("transport", "metadata", "structure", "runtime", "probe")


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


class Summary(BaseModel):
    """A page overview: each facet an optional section (``None`` when not
    requested / not applicable)."""

    transport: Transport | None = None
    metadata: Metadata | None = None
    structure: Structure | None = None
    runtime: Runtime | None = None
    probe: Probe | None = None


__all__ = [
    "Summary",
    "Transport",
    "Metadata",
    "Structure",
    "Runtime",
    "Probe",
    "TocEntry",
    "Form",
    "XhrCall",
    "FACETS",
]
