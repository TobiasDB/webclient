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
from urllib.parse import parse_qsl, urlencode, urlparse, urlsplit, urlunsplit

from ..web_core import Backing
from .models import DEFAULT_FACETS, Edge

if TYPE_CHECKING:
    from urllib.robotparser import RobotFileParser

    from . import Crawl

#: query params that never identify a distinct page (tracking / analytics); dropped
#: when canonicalising so ``/p?utm_source=x`` and ``/p`` are the same crawl target.
_TRACKING = frozenset(
    {"fbclid", "gclid", "gclsrc", "dclid", "msclkid", "mc_eid", "igshid"}
)  # unambiguous analytics params; ``ref``/``ref_src`` are left in (can be meaningful)


def _fold_host(host: str) -> str:
    """Lowercase a bare host and fold ``www.`` into the apex."""
    host = (host or "").lower()
    return host[4:] if host.startswith("www.") else host


def _canon_host(url: str) -> str:
    """The folded host of a URL, for scope comparison."""
    return _fold_host(urlparse(url).hostname or "")


def _canon(url: str) -> str:
    """A canonical dedup key: lowercased scheme+host (``www.`` folded, default port
    dropped), normalised trailing slash, tracking params removed and the rest
    sorted, fragment stripped -- so ``/p``, ``/p/``, ``/p?utm=1`` and ``//WWW.H/p``
    collapse to one key (and are not re-fetched)."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    host = _canon_host(url)
    netloc = f"{host}:{parts.port}" if parts.port and parts.port not in (80, 443) else host
    path = parts.path or "/"
    if len(path) > 1:
        path = path.rstrip("/") or "/"
    query = urlencode(
        sorted(
            (k, v)
            for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if k not in _TRACKING and not k.startswith("utm_")
        )
    )
    return urlunsplit(((parts.scheme or "https").lower(), netloc, path, query, ""))


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
        # only take (and remove from the frontier) what the page budget allows, so a
        # nearly-full budget doesn't silently discard the un-fetched chosen edges --
        # they stay in the frontier for the next step.
        room = max(0, core.max_pages - len(core.pages))
        to_fetch = chosen[:room]
        taken = {e.url for e in to_fetch}
        core.frontier = [e for e in core.frontier if e.url not in taken]
        for edge in to_fetch:
            if core.obey_robots and not await self._allowed(core, edge.url):
                continue
            doc = await core._client.afetch(
                core._client.ref(edge.url), optional=True, browser=core.browser
            )
            if not doc.ok:
                continue
            if core.browser:  # captured the render + its XHR events; free the page
                await core._client._arelease(doc)  # (also lets select run in-memory)
            # the crawl decides which backings populate each page's summary
            # (``facets`` empty -> the lean DEFAULT_FACETS, not every facet, since
            # a full summary per page is wasteful at crawl scale).
            core.pages.append(doc.summary(*(core.facets or DEFAULT_FACETS)))
            if edge.depth < core.max_depth and doc.kind in ("html", "xml"):
                self._expand(core, doc, edge.depth + 1)
            if core.browser and edge.depth < core.max_depth:
                # a browser render observed the page's XHR/fetch calls -- add those
                # data-API endpoints to the frontier so the crawl covers them too.
                self._expand_xhr(core, doc, edge.depth + 1)
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

    def _add_edge(self, core: "Crawl", url: str, text: str, depth: int) -> None:
        """Add one discovered URL to the frontier if it is in scope, matches
        include/exclude, and its canonical form has not been seen (so URL variants
        -- trailing slash, tracking params, www -- are not re-fetched)."""
        url = url.split("#", 1)[0]
        if not url.startswith(("http://", "https://")):
            return
        key = _canon(url)
        if key in core._seen:
            return
        if core.same_origin and _canon_host(url) != _fold_host(core.scope):
            return
        path = _path(url)
        if core.include is not None and core.include not in path:
            return
        if core.exclude is not None and core.exclude in path:
            return
        core._seen.add(key)
        core.frontier.append(Edge(url=url, text=text, depth=depth))

    def _expand(self, core: "Crawl", doc: Any, depth: int) -> None:
        """Add ``doc``'s anchor links to the frontier (anchor text kept for keyword
        scoring)."""
        for a in doc.select_all("a[href]"):
            self._add_edge(core, str(a.attr("href").url), (a.text_content or "").strip(), depth)

    def _expand_xhr(self, core: "Crawl", doc: Any, depth: int) -> None:
        """Add the data-API endpoints a browser render observed (the page's XHR /
        fetch calls) to the frontier -- so a crawl using the browser covers the
        JSON APIs behind the page, not only its anchor links."""
        from ...models import NetworkEvent

        for e in doc.events_of(NetworkEvent):
            if getattr(e, "resource_type", None) not in ("xhr", "fetch"):
                continue
            req = e.request
            url = str(req.dispatch("url")) if req is not None else ""
            if url:
                self._add_edge(core, url, "[xhr]", depth)

    # -- robots.txt (cached per host) ----------------------------------------
    async def _allowed(self, core: "Crawl", url: str) -> bool:
        host = _canon_host(url)
        if host not in core._robots:  # load this host's robots.txt once
            core._robots[host] = await self._load_robots(core, url)
        robots: "RobotFileParser | None" = core._robots[host]
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
