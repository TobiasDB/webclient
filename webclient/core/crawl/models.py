"""Crawl's model + interface.

``ICrawl`` is a site traversal's state: its options (scope / bounds / keywords) and
its live state -- the ``frontier`` (unresolved :class:`Edge` links) and the ``pages``
it has fetched (the resolved :class:`Document`\\ s, so their content is an expression:
``doc.attr("text")``, ``doc.flags()``, ``doc.extract(...).project()``) -- plus,
under ``TYPE_CHECKING``, the ops it implements (``step`` / ``run`` / ``done``).
``Crawl`` inherits it and holds the dedup/scope machinery.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, Field

from ..document.models import PageCard  # the default projection's output (re-exported)
from ..reference.models import Resolve

if TYPE_CHECKING:
    from . import Crawl  # noqa: F401  (step/run return the crawl itself)


def _default_project() -> Any:
    """The default retention expression: ``doc.card()`` -- a serializable, always-used
    projection that yields a :class:`PageCard` per page (built lazily so importing the
    lazy ``doc`` root doesn't cycle at module load)."""
    from ...surfaces import doc

    return doc.card()


class Edge(BaseModel):
    """An unresolved frontier edge: a discovered-but-not-yet-fetched link. ``text``
    is the anchor text (the keyword-relevance signal); ``depth`` is its distance
    from the seeds; ``score`` is a discovery-time importance (a weighted mix of
    keyword match, URL length + path entropy, and the link's on-page prominence) --
    the frontier is kept sorted by it in auto mode, so the useful links lead."""

    url: str
    text: str = ""
    depth: int = 0
    score: float = 0.0


class ScoreWeights(BaseModel):
    """Weights for the frontier scorer (all overridable). Higher = more influence."""

    keyword: float = 4.0  # per keyword hit in anchor text + URL (dominates importance)
    prominence: float = 1.0  # the link's on-page position + host-element size
    editorial: float = 0.8  # an article/content URL shape (slug / date)
    url_length: float = 0.15  # penalty per ~10 chars of URL over a short baseline
    url_entropy: float = 0.4  # penalty for high path entropy (opaque / tracking URLs)
    depth: float = 0.05  # penalty per level from the seed
    boiler: float = 1.2  # penalty for legal / social / pagination boilerplate


class CrawlConfig(BaseModel):
    """Everything that steers a crawl, grouped and documented so the options are
    discoverable and the defaults obvious. Build it explicitly for full control, or
    let ``wc.crawl(seeds, **kwargs)`` assemble it from keyword arguments (a passed
    ``config=`` wins, kwargs override it field-by-field). ``model_config`` allows the
    ``project`` callable + ``Resolve`` to live on it."""

    model_config = {"arbitrary_types_allowed": True}

    # -- budget (how much to fetch) ------------------------------------------
    max_pages: int = 50  # total pages fetched
    max_depth: int = 3  # link distance from the seeds
    width: int = 10  # auto: frontier edges expanded per round
    max_frontier: int = 10_000  # hard cap on retained unresolved edges

    # -- scope (what enters the frontier) ------------------------------------
    same_origin: bool = True  # stay on the seed's registrable domain
    allow_subdomains: bool = True  # ...but allow its subdomains (news./blog./www.)
    allow_domains: list[str] = []  # extra registrable domains to allow (cross-site)
    deny_domains: list[str] = []  # registrable domains to always reject
    allow_countries: list[str] = []  # allow only these ccTLDs (e.g. ["us", "gb"]); empty = any
    deny_countries: list[str] = []  # reject these ccTLDs
    include: str | None = None  # only follow links whose path contains this
    exclude: str | None = None  # skip links whose path contains this
    include_xhr: bool = True  # add a browser render's data-API (XHR/fetch) endpoints
    keywords: list[str] = []  # relevance heuristic (best-first + a soft filter)
    obey_robots: bool = True

    # -- fetch ---------------------------------------------------------------
    #: render tier per page. ``"auto"`` (default) fetches static first and escalates
    #: to a browser only when the page's flags say so (JS-gated / blocked) -- best of
    #: both worlds. ``True``/``"always"`` renders every page; ``False``/``"never"`` is
    #: pure-static (fastest).
    browser: "bool | Literal['never', 'auto', 'always']" = "auto"
    resolve: Resolve | None = None  # the resiliency policy bundle (retry/rate/proxy/...)

    # -- ordering ------------------------------------------------------------
    #: ``"best-first"`` expands the top-``width`` scored edges each round (auto's
    #: default); ``"manual"`` fetches exactly what ``step(select)`` is given (scores
    #: are still attached for the caller's own ranking).
    order: Literal["best-first", "manual"] = "best-first"
    scoring: ScoreWeights = ScoreWeights()

    # -- retention (what ends up in .pages) ----------------------------------
    #: the projection expression evaluated against every fetched page -- ``.pages`` is
    #: its result. A serializable ``Expr`` rooted at the document (so it runs the same
    #: locally and on a remote server), defaulting to ``doc.card()`` (a lean
    #: :class:`PageCard`). Pass any document expression to reshape retention --
    #: ``wq.doc.markdown()``, ``wq.doc.extract(...).project()``, or ``wq.doc`` (the
    #: identity, to keep whole Documents).
    project: Any = Field(default_factory=_default_project)


class CrawlState(BaseModel):
    """A resumable snapshot of a crawl: its config, the unresolved frontier, the
    canonical URLs already seen, and the history of edges taken. ``wc.crawl(seeds,
    resume=state)`` rehydrates it so a crawl continues where it stopped."""

    model_config = {"arbitrary_types_allowed": True}

    config: CrawlConfig = CrawlConfig()
    scope: str = ""  # the crawl's bound registrable domain
    frontier: list[Edge] = []
    seen: list[str] = []  # canonical URLs already fetched or queued
    history: list[Edge] = []  # the edges taken, in order (the resume trail)


class ICrawl(BaseModel):
    """A site traversal's state: its :class:`CrawlConfig` + the live frontier / pages
    / trace, plus (for the checker) the ops ``Crawl`` implements. The ops are
    ``TYPE_CHECKING``-only, so at runtime this is just the state model. Read the tuning
    knobs on ``.config``; ``scope`` (the seed's host) is derived, not configured."""

    model_config = {"arbitrary_types_allowed": True}

    config: CrawlConfig = CrawlConfig()  # all the tuning knobs (grouped + documented)
    scope: str = ""  # the registrable domain the crawl is bound to (derived from seeds)
    status: Literal["running", "closed"] = "running"
    # -- live state ----------------------------------------------------------
    #: the retained pages -- a lean :class:`PageCard` each by default, or the whole
    #: :class:`Document` when ``config.retain == "document"`` (untyped so pydantic
    #: never copies a live Document).
    pages: list[Any] = []
    #: unresolved edges (deduped, in scope), kept sorted best-first in auto mode.
    frontier: list[Edge] = []
    #: the edges taken, in order -- the audit trail + the resume history.
    history: list[Edge] = []

    def __str__(self) -> str:
        """An LLM/human-readable digest: status, the pages crawled, and the top of the
        frontier -- so ``print(crawl)`` is useful without digging through the lists."""
        head = (
            f"crawl [{self.status}] scope={self.scope or '-'} · "
            f"{len(self.pages)} page(s), {len(self.frontier)} frontier link(s)"
        )
        lines = [head]
        if self.pages:
            lines.append("pages:")
            for page in self.pages[:10]:
                url = getattr(page, "final_url", None) or getattr(page, "url", "?")
                code = getattr(page, "status_code", "?")
                title = getattr(page, "title", None)
                if callable(title):  # a Document's title is a prop; a PageCard's is a value
                    title = None
                lines.append(f"  [{code}] {url}" + (f" — {title}" if title else ""))
            if len(self.pages) > 10:
                lines.append(f"  … +{len(self.pages) - 10} more")
        if self.frontier:
            lines.append("frontier (best first):")
            for e in self.frontier[:10]:
                label = e.text[:38] if e.text else "—"
                lines.append(f"  {e.score:6.2f}  {label!r:40}  {e.url}")
            if len(self.frontier) > 10:
                lines.append(f"  … +{len(self.frontier) - 10} more")
        return "\n".join(lines)

    if TYPE_CHECKING:
        # >>> generated: Crawl interface <<<
        # fmt: off
        @property
        def done(self) -> bool: ...
        def run(self) -> "Crawl": ...
        def step(self, select: 'list[Edge] | list[str] | None' = ...) -> "Crawl": ...
        # fmt: on
        # >>> end generated <<<
        pass


__all__ = ["Edge", "PageCard", "ScoreWeights", "CrawlConfig", "CrawlState", "ICrawl"]
