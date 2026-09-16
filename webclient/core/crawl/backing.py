"""CrawlBacking: the site-traversal ops for a :class:`Crawl` core.

``step`` fetches one round: the caller's selection (frontier edges, their URLs, OR
brand-new URLs), or -- in best-first order -- the top-``width`` scored edges. Each
fetched page is retained (a lean :class:`PageCard` projection, or the whole Document
under ``retain="document"``) and its in-scope, deduped, robots-allowed links expand
the frontier. ``run`` drives ``step`` to completion. Built ON the interface -- the
client's ``afetch`` for transport, the document's ``select_all``/``attr``/facets for
discovery + projection -- so a crawl is one backing over existing cores.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, AsyncIterator, Callable, cast
from urllib.parse import urlparse, urlsplit

from ..web_core import Backing
from .canon import (  # URL canon / scope / scoring vocabulary (pure helpers)
    _BOILER_PATH_RE,
    _BOILER_RE,
    _CTA,
    _DATE_RE,
    _EDITORIAL,
    _REGION_WEIGHT,
    _RESOURCE_EXT,
    _SOCIAL_HOSTS,
    _canon,
    _canon_host,
    _cctld,
    _ext,
    _is_paginated,
    _path,
    _registrable,
    _url_entropy,
)
from .models import Edge, Failure

if TYPE_CHECKING:
    from urllib.robotparser import RobotFileParser

    from ..document import Document
    from . import Crawl


class CrawlBacking(Backing):
    """The traversal ops: ``step`` (one round), ``run`` (to completion), and the
    ``done`` predicate. ``step``/``run`` fetch, so they are IO ops (the interface
    bridges them onto the client's sync/async dispatcher)."""

    provides = frozenset({"step", "run"})
    props = frozenset({"done"})
    io = frozenset({"step", "run"})
    gate = "ok"

    def done(self, core: "Crawl") -> bool:
        """Finished: closed, the frontier is empty, or the page budget is spent."""
        return (
            core.status == "closed"
            or not core.frontier
            or len(core.pages) >= core.config.max_pages
        )

    async def aexit(self, core: "Crawl", *exc: Any) -> None:
        """Close the crawl when its ``with`` block exits."""
        core.status = "closed"

    async def step(
        self, core: "Crawl", select: "list[Edge] | list[str] | None" = None
    ) -> "Crawl":
        """Fetch one round. ``select`` is a subset of the frontier (edges or URLs) OR
        brand-new URLs to fetch next (any not in the frontier are added, bypassing the
        scope filters -- an explicit ask wins); ``None`` takes the top-``width`` scored
        edges in best-first order, or nothing in ``manual`` order. Each fetched page is
        retained per ``config.retain`` and its links expand the frontier.

        A per-crawl lock serialises rounds so the frontier-claim + budget + expansion
        is atomic -- concurrently-awaited steps can't each claim the full budget."""
        await self._pump(core, lambda: self._select(core, select))
        return core

    async def run(self, core: "Crawl") -> "Crawl":
        """Drive the crawl to completion (the batch drain of the stream): expand the
        best-first frontier round by round until done. Equivalent to exhausting
        ``stream()`` -- ``config.order`` only governs a bare ``step()``, not the drive."""
        while True:
            produced = await self._pump(core, lambda: self._drive_select(core))
            if self.done(core) or not produced:
                break
        return core

    async def _astream(self, core: "Crawl") -> "AsyncIterator[Any]":
        """The crawl's one engine: drive the best-first frontier round by round and
        yield each fetched page's retained projection as the round completes. Pausing
        (breaking the consumer) leaves the frontier + seen ledger intact, so the crawl
        is resumable; ``run`` is this stream drained. Each round's claim/fetch/expand is
        atomic under the step lock; the yield happens after the lock is released, so a
        paused consumer never holds it."""
        while not self.done(core):
            produced = await self._pump(core, lambda: self._drive_select(core))
            for page in produced:
                yield page
            if not produced:  # no progress (all robots-blocked / errored) -- stop
                break

    async def _pump(
        self, core: "Crawl", choose: "Callable[[], list[Edge]]"
    ) -> "list[Any]":
        """One round, atomic under the step lock: select+claim the edges (``choose`` runs
        under the lock so concurrent rounds can't pick the same edges or over-claim the
        page budget), fetch each, and append the retained projections. Returns the pages
        produced this round (for the stream to yield)."""
        async with self._lock(core):
            chosen = choose()
            room = max(0, core.config.max_pages - len(core.pages))
            to_fetch = chosen[:room]
            taken = {e.url for e in to_fetch}
            core.frontier = [e for e in core.frontier if e.url not in taken]
            produced: list[Any] = []
            for edge in to_fetch:
                page = await self._fetch_edge(core, edge)
                if page is not None:
                    core.pages.append(page)
                    produced.append(page)
            return produced

    async def _fetch_edge(self, core: "Crawl", edge: Edge) -> Any:
        """Fetch one edge, expand the frontier from its DOM, and return its retained
        projection (``None`` if robots-blocked or the fetch failed -- a
        :class:`Failure` is recorded either way, so the crawl degrades gracefully).
        The audit/resume trail records every edge actually taken."""
        if core.config.obey_robots and not await self._allowed(core, edge.url):
            core.failures.append(
                Failure(url=edge.url, reason="robots-disallowed", depth=edge.depth)
            )
            return None
        # The WHOLE edge -- fetch, DOM expansion, and projection -- is guarded: a transport
        # error can surface not just from the fetch but while reading a live page (expanding
        # its links / XHR, projecting its card), and none of those may abort the crawl. One
        # bad edge becomes one Failure; the page is always released.
        doc: Any = None
        try:
            doc = await core._client.afetch(
                core._client.ref(edge.url),
                optional=True,
                browser=core.config.browser,
                resolve=core.config.resolve,
            )
            core.history.append(edge)  # the audit + resume trail (every edge taken)
            if not doc.ok:
                err = getattr(doc, "error", None)
                core.failures.append(Failure(
                    url=edge.url,
                    reason=err.type if err is not None else "not-ok",
                    status_code=doc.status_code or (err.status_code if err is not None else None),
                    depth=edge.depth,
                ))
                return None
            # expand the frontier BEFORE releasing the page (needs the DOM), then project.
            if edge.depth < core.config.max_depth and doc.kind in ("html", "xml"):
                self._expand(core, doc, edge.depth + 1)
            if core.config.include_xhr and edge.depth < core.config.max_depth:
                self._expand_xhr(core, doc, edge.depth + 1)
            return await self._retain(core, doc)  # project while the page is still live
        except Exception as exc:  # never let one bad edge abort the whole crawl
            core.failures.append(
                Failure(url=edge.url, reason=type(exc).__name__, depth=edge.depth)
            )
            return None
        finally:
            if doc is not None and getattr(doc, "_page", None) is not None:
                await core._client._arelease(doc)

    def _lock(self, core: "Crawl") -> "asyncio.Lock":
        """The crawl's step lock, created lazily on its running loop (the sync check +
        assign means even the first two concurrent steps share one lock)."""
        if core._step_lock is None:
            core._step_lock = asyncio.Lock()
        return cast("asyncio.Lock", core._step_lock)

    # -- retention ------------------------------------------------------------
    async def _retain(self, core: "Crawl", doc: "Document") -> Any:
        """Evaluate the crawl's projection expression against the fetched page and keep
        the result -- ``config.project`` is a document-rooted :class:`Expr` (default
        ``doc.card()`` -> a :class:`PageCard`), so ``.pages`` is that expression's
        output. Read while the page is still live (before its browser page is freed)."""
        from ...query.executor import aevaluate

        return await aevaluate(core.config.project, doc, client=core._client)

    # -- frontier selection + scoring -----------------------------------------
    def _select(self, core: "Crawl", select: Any) -> "list[Edge]":
        if select is not None:
            wanted = [s.url if isinstance(s, Edge) else str(s) for s in select]
            existing = {e.url for e in core.frontier}
            for u in wanted:  # brand-new URLs the caller supplied: add them (forced)
                if u not in existing:
                    self._add_edge(core, u, "", 0, 0.0, force=True)
            want = set(wanted)
            return [e for e in core.frontier if e.url in want]
        if core.config.order == "manual":
            return []  # manual: a bare step() fetches nothing -- the caller selects
        return self._drive_select(core)

    def _drive_select(self, core: "Crawl") -> "list[Edge]":
        """The auto drive's selection: the top-``width`` frontier edges by score. Used by
        ``run``/``stream`` regardless of ``config.order`` -- the drives are always
        best-first; ``order`` only governs what a bare ``step()`` does."""
        ranked = sorted(core.frontier, key=lambda e: self._score(core, e), reverse=True)
        return ranked[: core.config.width]

    def _score(self, core: "Crawl", edge: Edge) -> float:
        """Best-first ordering: the edge's discovery-time score minus a depth penalty
        (ties break toward shallower pages)."""
        return edge.score - core.config.scoring.depth * edge.depth

    def _link_score(self, core: "Crawl", text: str, url: str, region: str) -> float:
        """A weighted metric score for a discovered link (weights on
        ``config.scoring``): keyword matches (anchor + URL), on-page prominence (the
        region landmark -- the available proxy for the host element's position/size,
        since true pixel size needs a layout), an editorial URL shape, minus URL
        length + path-entropy penalties and legal/social/pagination boilerplate."""
        w = core.config.scoring
        t = " ".join(text.split()).lower()
        path = _path(url).lower()
        score = w.prominence * _REGION_WEIGHT.get(region, 0.0)

        if core.config.keywords:
            blob = f"{t} {path}"
            score += w.keyword * sum(blob.count(k.lower()) for k in core.config.keywords)

        if any(seg in path for seg in _EDITORIAL):
            score += w.editorial
        if _DATE_RE.search(path):
            score += w.editorial * 0.5
        last = path.rstrip("/").rsplit("/", 1)[-1]
        if "-" in last and len(last) > 8 and "." not in last:  # a content slug
            score += w.editorial * 0.6

        if not t:  # an icon / image link -- no label to act on
            score -= w.prominence
        elif any(c in t for c in _CTA):  # "read more" / "continue reading" ...
            score += w.keyword * 0.4
        if t and _BOILER_RE.search(t):
            score -= w.boiler

        score -= w.url_length * max(0.0, (len(url) - 60) / 10)
        score -= w.url_entropy * max(0.0, _url_entropy(url) - 3.5)
        if _BOILER_PATH_RE.search(path):
            score -= w.boiler
        if _canon_host(url) in _SOCIAL_HOSTS:
            score -= w.boiler * 1.2
        if _is_paginated(url):
            score -= w.boiler * 0.6
        return round(score, 3)

    # -- frontier growth ------------------------------------------------------
    def _in_scope(self, core: "Crawl", url: str) -> bool:
        """Whether ``url`` may enter the frontier under the config's scope rules --
        domain allow/deny, country (ccTLD) allow/deny, same-origin + subdomains, and
        the include/exclude path filters."""
        cfg = core.config
        host = (urlparse(url).hostname or "").lower()
        reg = _registrable(host)
        if reg in cfg.deny_domains:
            return False
        cc = _cctld(host)
        if cc and cc in cfg.deny_countries:
            return False
        if cfg.allow_countries and cc not in cfg.allow_countries:
            return False
        on_site = reg == _registrable(core.scope) or reg in cfg.allow_domains
        if not cfg.allow_subdomains and host != core.scope.lower():
            on_site = on_site and host == core.scope.lower()
        if cfg.same_origin and not on_site:
            return False
        path = _path(url)
        if cfg.include is not None and cfg.include not in path:
            return False
        if cfg.exclude is not None and cfg.exclude in path:
            return False
        return True

    def _add_edge(
        self, core: "Crawl", url: str, text: str, depth: int, score: float = 0.0,
        *, force: bool = False,
    ) -> None:
        """Add one discovered URL to the frontier if unseen and (unless ``force``) in
        scope. ``force`` is for caller-supplied URLs in ``step`` -- an explicit ask
        bypasses the scope filters but still dedups by canonical key."""
        url = url.split("#", 1)[0]
        if not url.startswith(("http://", "https://")):
            return
        try:
            urlsplit(url).port  # skip an unfetchable URL (bad/out-of-range port)
        except ValueError:
            return
        key = _canon(url)
        if key in core._seen:
            return
        if not force and not self._in_scope(core, url):
            return
        core._seen.add(key)
        core.frontier.append(Edge(url=url, text=text, depth=depth, score=score))

    def _expand(self, core: "Crawl", doc: Any, depth: int) -> None:
        """Add ``doc``'s anchor links to the frontier -- dropping page-asset links and
        scoring each by the metric scorer -- then re-sort/cap the frontier."""
        for a in doc.select_all("a[href]"):
            url = str(a.attr("href").url)
            if _ext(_path(url)) in _RESOURCE_EXT:  # a resource link, not a page
                continue
            text = (a.text_content or "").strip()
            self._add_edge(core, url, text, depth, self._link_score(core, text, url, a.region))
        self._sort_frontier(core)

    def _expand_xhr(self, core: "Crawl", doc: Any, depth: int) -> None:
        """Add the data-API endpoints a browser render observed (its XHR/fetch calls)
        to the frontier, so a browser crawl covers the JSON APIs behind the page."""
        from ...models import NetworkEvent

        for e in doc.events_of(NetworkEvent):
            if getattr(e, "resource_type", None) not in ("xhr", "fetch"):
                continue
            req = e.request
            url = str(req.dispatch("url")) if req is not None else ""
            if url:  # a data-API endpoint -- rides mid-frontier, not sunk as a resource
                self._add_edge(core, url, "[xhr]", depth, 0.5)
        self._sort_frontier(core)

    def _sort_frontier(self, core: "Crawl") -> None:
        """Keep the frontier best-first (score desc, then shallowest) and hard-capped
        at ``config.max_frontier`` -- a small page budget can still discover hundreds of
        links per page, so the cap bounds memory while keeping the best edges."""
        core.frontier.sort(key=lambda e: (-e.score, e.depth))
        if len(core.frontier) > core.config.max_frontier:
            del core.frontier[core.config.max_frontier :]

    # -- robots.txt (cached per host) -----------------------------------------
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
            core._client.ref(f"{p.scheme}://{p.netloc}/robots.txt"),
            optional=True,
            resolve=core.config.resolve,
        )
        if not doc.ok or not doc.content:
            return None
        rp = RobotFileParser()
        rp.parse(doc.content.decode("utf-8", "replace").splitlines())
        return rp


__all__ = ["CrawlBacking"]
