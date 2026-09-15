"""Crawl's model + interface.

``ICrawl`` is a site traversal's state: its options (scope / bounds / keywords)
and its live state -- the ``frontier`` (unresolved edges) and the ``pages`` it has
fetched (each an LLM-efficient :class:`Summary`) -- plus, under ``TYPE_CHECKING``,
the ops it implements (``step`` / ``run`` / ``done``). ``Crawl`` inherits it and
holds the dedup/scope machinery; the ops are ``TYPE_CHECKING``-only so at runtime
this is just the state model and ``WebCore.__getattr__`` dispatches every op.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, Field, model_validator

from ..document.models import Summary
from ..reference.models import Resolve

if TYPE_CHECKING:
    from . import Crawl  # noqa: F401  (step/run return the crawl itself)


#: the default set of summary facets a crawl carries per page when ``facets`` is
#: not set -- tuned for the crawl's main user, an LLM mapping a site and deciding
#: which pages to read next. It carries the three facets that answer "is this page
#: ok, what is it, and what's on it": transport (url/status/kind), metadata
#: (title/description/canonical), and structure (headings/TOC, word count, links,
#: forms, pagination). ``structure`` is nearly free here -- the crawl already parses
#: every page's DOM to expand its links, so the facet just reads the cached tree.
#: The browser-only facets (runtime/probe) are excluded: they only apply after a
#: render/escalation, so on a static crawl they are always empty anyway. Pass
#: ``facets=[...]`` to widen (``facets=list(FACETS)`` for everything, adding runtime
#: on a browser crawl) or narrow it (``facets=["metadata"]`` for the leanest).
DEFAULT_FACETS = ("transport", "metadata", "structure")

#: facets added to the default on a *browser* crawl -- ``runtime`` (SPA / framework
#: / XHR-endpoint signals), which only carries data once a page has actually been
#: rendered, so it is pointless on a static crawl but valuable on a browser one.
BROWSER_FACETS = ("runtime",)


def default_facets(*, browser: bool = False) -> list[str]:
    """The default per-page summary facets for a crawl, given its transport tier.
    Always the three "is it ok / what is it / what's on it" facets
    (:data:`DEFAULT_FACETS`); a browser crawl additionally gets :data:`BROWSER_FACETS`
    (``runtime``), since a render is what makes those signals available."""
    facets: list[str] = list(DEFAULT_FACETS)
    if browser:
        facets += list(BROWSER_FACETS)
    return facets


class Edge(BaseModel):
    """An unresolved frontier edge: a discovered-but-not-yet-fetched link. ``text``
    is the anchor text (the keyword-relevance signal); ``depth`` is its distance
    from the seeds; ``score`` is a discovery-time importance (nav / "read more" /
    article links score high, footer / legal / social / icon links low) -- the
    frontier is kept sorted by it, so the useful links surface first."""

    url: str
    text: str = ""
    depth: int = 0
    score: float = 0.0


class ICrawl(BaseModel):
    """A site traversal's state: the options + the live frontier/pages, plus (for
    the checker) the ops ``Crawl`` implements. The ops are ``TYPE_CHECKING``-only,
    so at runtime this is just the state model."""

    # -- options -------------------------------------------------------------
    scope: str = ""  # the host the crawl is bound to (when same_origin)
    #: self-drive: each ``step`` (or ``run``) expands the top-``width`` frontier
    #: edges best-first. On by default -- the common case is "map this site", not
    #: hand-stepping the frontier; pass ``auto=False`` to drive rounds yourself.
    auto: bool = True
    width: int = 10  # auto: how many frontier edges to expand per round
    max_depth: int = 3
    max_pages: int = 50
    same_origin: bool = True
    obey_robots: bool = True
    #: render each page in a browser (so JS/lazy-loaded links & content are seen,
    #: and the page's XHR/data-API calls are captured and added to the frontier).
    #: On by default -- most sites today are JS-heavy, and a static crawl silently
    #: misses their links (the failure this fixes). Needs Playwright; pass
    #: ``browser=False`` for a pure-static, no-render crawl.
    browser: bool = True
    keywords: list[str] = []  # best-first relevance signal (auto mode)
    include: str | None = None  # only follow links whose path contains this
    exclude: str | None = None  # skip links whose path contains this
    #: the resiliency policy bundle (retry / rate / proxy / anti-bot / browser) the
    #: crawl fetches under. ``None`` inherits the client's own ``resolve``; set it to
    #: give a crawl its own policy (e.g. ``Resolve.auto()``, or a proxy pool).
    resolve: Resolve | None = None
    #: which summary backings each fetched page carries. Defaults (here, on the
    #: model -- not applied deep in ``step``) to ``DEFAULT_FACETS`` (plus ``runtime``
    #: when ``browser`` -- see the validator below); pass ``list(FACETS)`` for the
    #: full summary, or any subset of facet/backing names.
    facets: list[str] = Field(default_factory=lambda: list(DEFAULT_FACETS))
    status: Literal["running", "closed"] = "running"

    @model_validator(mode="after")
    def _add_browser_facets(self) -> "ICrawl":
        """A browser crawl left on the default facets also carries the browser-only
        facets (``runtime``): a render is what makes those signals real. An explicit
        ``facets=[...]`` is respected as-is. Idempotent (safe on remote re-hydration)
        -- it only fires while ``facets`` is still exactly ``DEFAULT_FACETS``."""
        if self.browser and self.facets == list(DEFAULT_FACETS):
            self.facets = default_facets(browser=True)
        return self
    # -- live state (the LLM-efficient output) -------------------------------
    pages: list[Summary] = []  # a .summary() per fetched page
    frontier: list[Edge] = []  # unresolved edges (deduped, in scope)

    def __str__(self) -> str:
        """An LLM/human-readable digest: status, the pages crawled (status + title),
        and the top of the scored frontier -- so ``print(crawl)`` is useful without
        digging through ``.pages`` / ``.frontier`` by hand."""
        head = (
            f"crawl [{self.status}] scope={self.scope or '-'} · "
            f"{len(self.pages)} page(s), {len(self.frontier)} frontier link(s)"
        )
        lines = [head]
        if self.pages:
            lines.append("pages:")
            for pg in self.pages[:10]:
                t = pg.transport
                url = t.final_url if t else "?"
                code = t.status_code if t else "?"
                title = pg.metadata.title if pg.metadata else None
                # a compact substance hint from the structure facet (word count),
                # so the scan shows which pages carry real content vs. thin ones.
                wc = pg.structure.word_count if pg.structure else None
                tail = (f" — {title}" if title else "") + (f" ({wc}w)" if wc else "")
                lines.append(f"  [{code}] {url}{tail}")
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


__all__ = ["BROWSER_FACETS", "DEFAULT_FACETS", "Edge", "ICrawl", "default_facets"]
