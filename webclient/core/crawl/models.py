"""Crawl's model + interface.

``ICrawl`` is a site traversal's state: its options (scope / bounds / keywords) and
its live state -- the ``frontier`` (unresolved :class:`Edge` links) and the ``pages``
it has fetched (the resolved :class:`Document`\\ s, so their content is an expression:
``doc.attr("text")``, ``doc.runtime()``, ``doc.extract(...).project()``) -- plus,
under ``TYPE_CHECKING``, the ops it implements (``step`` / ``run`` / ``done``).
``Crawl`` inherits it and holds the dedup/scope machinery.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel

from ..reference.models import Resolve

if TYPE_CHECKING:
    from . import Crawl  # noqa: F401  (step/run return the crawl itself)


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

    model_config = {"arbitrary_types_allowed": True}

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
    #: misses their links. Needs Playwright; pass ``browser=False`` for a pure-static
    #: crawl.
    browser: bool = True
    keywords: list[str] = []  # best-first relevance signal (auto mode)
    include: str | None = None  # only follow links whose path contains this
    exclude: str | None = None  # skip links whose path contains this
    #: the resiliency policy bundle (retry / rate / proxy / anti-bot / browser) the
    #: crawl fetches under. ``None`` inherits the client's own ``resolve``.
    resolve: Resolve | None = None
    status: Literal["running", "closed"] = "running"
    # -- live state ----------------------------------------------------------
    #: the resolved pages, as :class:`Document`\\ s (kept, not projected -- extract
    #: whatever facet/content you want per page: ``doc.title`` / ``doc.runtime()`` /
    #: ``doc.markdown()`` / ``doc.extract(...).project()``). Stored untyped so pydantic
    #: never copies a live Document.
    pages: list[Any] = []
    #: unresolved edges (deduped, in scope), kept sorted best-first.
    frontier: list[Edge] = []

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
            for doc in self.pages[:10]:
                url = getattr(doc, "final_url", None) or getattr(doc, "url", "?")
                code = getattr(doc, "status_code", "?")
                title = doc.title if getattr(doc, "has_op", None) and doc.has_op("title") else None
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


__all__ = ["Edge", "ICrawl"]
