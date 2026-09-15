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

from pydantic import BaseModel, Field

from ..document.models import Summary

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
    auto: bool = False  # self-drive (best-first, top-`width` per round)
    width: int = 10  # auto: how many frontier edges to expand per round
    max_depth: int = 3
    max_pages: int = 50
    same_origin: bool = True
    obey_robots: bool = True
    browser: bool = False  # render each page in a browser (captures XHR/data-API
    #                        calls, which are then added to the frontier and crawled)
    keywords: list[str] = []  # best-first relevance signal (auto mode)
    include: str | None = None  # only follow links whose path contains this
    exclude: str | None = None  # skip links whose path contains this
    #: which summary backings each fetched page carries. Defaults (here, on the
    #: model -- not applied deep in ``step``) to the lean ``DEFAULT_FACETS``, since
    #: a full summary per page is wasteful at crawl scale; pass ``list(FACETS)`` for
    #: the full summary, or any subset of facet/backing names.
    facets: list[str] = Field(default_factory=lambda: list(DEFAULT_FACETS))
    status: Literal["running", "closed"] = "running"
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


__all__ = ["DEFAULT_FACETS", "Edge", "ICrawl"]
