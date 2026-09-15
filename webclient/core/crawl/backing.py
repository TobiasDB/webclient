"""CrawlBacking: the site-traversal ops for a :class:`Crawl` core.

``step`` fetches one round of the frontier (the caller's selection, or -- in auto
mode -- the top-``width`` edges best-first by keyword relevance), summarises each
page, and expands the frontier with its in-scope, deduped, robots-allowed links.
``run`` auto-drives ``step`` to completion. Built ON the interface -- the owning
client's ``afetch`` for transport, the document's ``select_all``/``attr`` for link
discovery -- so a crawl is one backing over existing cores.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from ..web_core import Backing
from .models import Edge

if TYPE_CHECKING:
    from urllib.robotparser import RobotFileParser

    from . import Crawl


def _host(url: str) -> str:
    return urlparse(url).hostname or ""


def _path(url: str) -> str:
    return urlparse(url).path or ""


class CrawlBacking(Backing):
    """The traversal ops: ``step`` (one round), ``run`` (auto to completion), and
    the ``done`` predicate. Both fetch, so they are IO ops (the interface bridges
    them onto the client's sync/async dispatcher)."""

    provides = frozenset({"step", "run"})
    props = frozenset({"done"})
    io = frozenset({"step", "run"})  # both fetch -- the interface bridges them
    gate = "ok"

    def done(self, core: "Crawl") -> bool:
        """Whether the crawl is finished: closed, the frontier is empty, or the
        page budget is spent."""
        return (
            core.status == "closed"
            or not core.frontier
            or len(core.pages) >= core.max_pages
        )

    async def aexit(self, core: "Crawl", *exc: Any) -> None:
        """Close the crawl when its ``with`` block exits."""
        core.status = "closed"

    async def step(
        self, core: "Crawl", select: "list[Edge] | list[str] | None" = None
    ) -> "Crawl":
        """Fetch one round. ``select`` (a subset of ``frontier`` -- edges or their
        URLs) chooses which edges to expand; ``None`` takes the top-``width`` edges
        best-first (by keyword relevance in auto mode, else shallowest-first). Each
        fetched page is summarised into ``pages`` and its links added to
        ``frontier``. Returns the crawl (so ``crawl.step()`` chains/reads)."""
        chosen = self._select(core, select)
        taken = {e.url for e in chosen}
        core.frontier = [e for e in core.frontier if e.url not in taken]
        room = core.max_pages - len(core.pages)
        for edge in chosen[: max(0, room)]:
            if core.obey_robots and not await self._allowed(core, edge.url):
                continue
            doc = await core._client.afetch(core._client.ref(edge.url), optional=True)
            if not doc.ok:
                continue
            # the crawl decides which backings populate each page's summary
            # (``facets`` empty -> every applicable facet).
            core.pages.append(doc.summary(*core.facets))
            if edge.depth < core.max_depth and doc.kind in ("html", "xml"):
                self._expand(core, doc, edge.depth + 1)
        return core

    async def run(self, core: "Crawl") -> "Crawl":
        """Auto-drive: ``step`` (top-``width`` best-first) each round until
        ``done``. Returns the finished crawl."""
        while not self.done(core):
            await self.step(core)
        return core

    # -- frontier selection + scoring ----------------------------------------
    def _select(self, core: "Crawl", select: Any) -> "list[Edge]":
        if select is not None:
            wanted = {s.url if isinstance(s, Edge) else str(s) for s in select}
            return [e for e in core.frontier if e.url in wanted]
        ranked = sorted(core.frontier, key=lambda e: self._score(core, e), reverse=True)
        return ranked[: core.width]

    def _score(self, core: "Crawl", edge: Edge) -> float:
        """Best-first relevance: keyword hits in the anchor text + URL (minus a
        tiny depth penalty to break ties toward shallower pages). With no keywords
        it degrades to shallowest-first (a breadth-first frontier)."""
        if not core.keywords:
            return float(-edge.depth)
        blob = f"{edge.text} {edge.url}".lower()
        return sum(blob.count(k) for k in core.keywords) - 0.001 * edge.depth

    def _expand(self, core: "Crawl", doc: Any, depth: int) -> None:
        """Add ``doc``'s links to the frontier: in-scope, matching include/exclude,
        not already seen. Anchor text is kept for keyword scoring."""
        for a in doc.select_all("a[href]"):
            url = str(a.attr("href").url).split("#", 1)[0]
            if url in core._seen:
                continue
            if core.same_origin and _host(url) != core.scope:
                continue
            path = _path(url)
            if core.include is not None and core.include not in path:
                continue
            if core.exclude is not None and core.exclude in path:
                continue
            core._seen.add(url)
            text = (a.text_content or "").strip()
            core.frontier.append(Edge(url=url, text=text, depth=depth))

    # -- robots.txt ----------------------------------------------------------
    async def _allowed(self, core: "Crawl", url: str) -> bool:
        if not core._robots_loaded:
            core._robots = await self._load_robots(core, url)
            core._robots_loaded = True
        robots: "RobotFileParser | None" = core._robots
        return robots is None or robots.can_fetch("*", url)

    async def _load_robots(self, core: "Crawl", sample_url: str) -> Any:
        from urllib.robotparser import RobotFileParser

        p = urlparse(sample_url)
        doc = await core._client.afetch(
            core._client.ref(f"{p.scheme}://{p.netloc}/robots.txt"), optional=True
        )
        if not doc.ok or not doc.content:
            return None
        rp = RobotFileParser()
        rp.parse(doc.content.decode("utf-8", "replace").splitlines())
        return rp


__all__ = ["CrawlBacking"]
