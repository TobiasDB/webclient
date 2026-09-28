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
import logging
from typing import TYPE_CHECKING, Any, AsyncIterator, Callable, cast
from urllib.parse import urlparse, urlsplit

from ...kernel.errors import make
from ...loop import BoundedLoop
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


log = logging.getLogger(__name__)

#: a round's terminal decision -- the frontier/budget is done (the real "which edges" decision is
#: the batch of :class:`Edge`\\ s that ``_claim`` returns; this sentinel just ends the loop).
_DONE: Any = object()


class CrawlBacking(Backing):
    """The traversal ops: ``step`` (one round), ``run`` (to completion), and the
    ``done`` predicate. ``step``/``run`` fetch, so they are IO ops (the interface
    bridges them onto the client's sync/async dispatcher)."""

    provides = frozenset({"step", "run"})
    props = frozenset({"done"})
    io = frozenset({"step", "run"})
    gate = "ok"

    def done(self, core: "Crawl[Any]") -> bool:
        """Finished: closed, the frontier is empty, or the page budget is spent."""
        return (
            core.status == "closed"
            or not core.frontier
            or len(core.pages) >= core.config.max_pages
        )

    async def aexit(self, core: "Crawl[Any]", *exc: Any) -> None:
        """Close the crawl when its ``with`` block exits."""
        core.status = "closed"

    async def step(
        self, core: "Crawl[Any]", select: "list[Edge] | list[str] | None" = None
    ) -> "Crawl[Any]":
        """Fetch one round. ``select`` is a subset of the frontier (edges or URLs) OR brand-new URLs
        to fetch next (any not in the frontier are added, bypassing the scope filters -- an explicit
        ask wins); ``None`` takes the top-``width`` scored edges in best-first order, or nothing in
        ``manual`` order. Each fetched page is retained and its links expand the frontier.

        One round of the crawl's :class:`BoundedLoop`, on a FRESH loop (so concurrently-awaited
        ``step``s don't race the loop's round state), with the claim + budget reservation atomic
        under the step lock -- so concurrent steps can't each claim the full page budget."""
        loop = self._make_loop(core)
        to_fetch = await self._claim(core, lambda: self._select(core, select))
        decision: Any = core._pending if core._pending is not None else to_fetch
        await loop.astep(core, decision=decision)
        return core

    def _make_loop(self, core: "Crawl[Any]") -> "BoundedLoop[Crawl[Any], Any, Any]":
        """The crawl as a :class:`~webclient.loop.BoundedLoop` (the ONE loop concept): each round
        DECIDES a batch of frontier edges (best-first, budget-reserved under the step lock) and
        APPLIES one FETCH per edge, which BoundedLoop fans out CONCURRENTLY (bounded by
        ``config.width`` -- the loop owns the efficiency). Stops when the frontier/budget is done
        (``decide`` -> :data:`_DONE`), a driver ``Ask`` pauses it, or a round fetches no new page
        (``max_stalls=1`` -- the old "not produced" break). Crawl STATE lives on the core, so
        ``resume``/re-``run`` continues from the current frontier."""

        async def decide(state: "Crawl[Any]") -> Any:
            if self.done(state):
                return _DONE
            to_fetch = await self._claim(state, lambda: self._drive_select(state))
            if state._pending is not None:  # the driver asked for a human -> checkpoint (waiting)
                return state._pending
            return to_fetch

        async def apply(state: "Crawl[Any]", edge: Edge) -> None:
            await self._apply_edge(state, edge)

        return BoundedLoop(
            observe=lambda s, i, e: s, decide=decide,
            done_result=lambda d: "done" if d is _DONE else None,
            apply=apply, progress=lambda s: len(s.pages),
            fanout=max(1, core.config.width),  # fetch the round's edges concurrently
            max_rounds=core.config.max_pages + 1,  # a safety cap; done()/stall stop first
            max_stalls=1,  # a round with no new page -> stop (the old "not produced" break)
            name="crawl", bus=getattr(core._client, "bus", None),
        )

    async def run(self, core: "Crawl[Any]") -> "Crawl[Any]":
        """Drive the crawl to completion (the batch drain of the stream): expand the best-first
        frontier round by round until done. Driven by a :class:`BoundedLoop` (:meth:`_make_loop`)."""
        await self._make_loop(core).arun(core)
        return core

    async def _astream(self, core: "Crawl[Any]") -> "AsyncIterator[Any]":
        """The crawl's one engine: drive the best-first frontier round by round (a
        :class:`BoundedLoop`) and yield each round's fetched pages as it completes. Pausing (breaking
        the consumer) leaves the frontier + seen ledger intact, so the crawl is resumable; ``run`` is
        this stream drained."""
        loop = self._make_loop(core)
        while True:
            seen = len(core.pages)
            verdict = await loop.astep(core)
            for page in core.pages[seen:]:
                yield page
            if verdict is not None:  # terminal (done / stalled / budget)
                return

    async def _claim(self, core: "Crawl[Any]", choose: "Callable[[], list[Edge]]") -> "list[Edge]":
        """The DECIDE half of a round: under the step lock, choose the edges (best-first / manual /
        forced), cap them to the remaining page budget, remove them from the frontier and RESERVE
        the budget via ``_inflight`` -- so concurrently-claimed rounds can't over-claim. Returns the
        claimed edges (the batch ``apply`` fetches); ``[]`` when the driver Asked or nothing is left."""
        async with self._lock(core):
            core._round += 1
            chosen = choose()
            if core._pending is not None:  # the driver asked for a human: claim nothing
                return []
            room = max(0, core.config.max_pages - len(core.pages) - core._inflight)
            to_fetch = chosen[:room]
            taken = {e.url for e in to_fetch}
            core.frontier = [e for e in core.frontier if e.url not in taken]
            core._inflight += len(to_fetch)  # reserve the budget for the in-flight fetches
            return to_fetch

    async def _apply_edge(self, core: "Crawl[Any]", edge: Edge) -> None:
        """The APPLY half (one unit of the batch, fanned out concurrently): FETCH + expand the edge
        (outside the lock, so the batch fetches in parallel), then commit its retained page + release
        its budget reservation under the lock. ``_fetch_edge`` records a Failure on a bad edge."""
        page = await self._fetch_edge(core, edge)
        async with self._lock(core):
            if page is not None:
                core.pages.append(page)
            core._inflight -= 1

    def _emit(self, core: "Crawl[Any]", phase: str, **detail: Any) -> None:
        """Publish a :class:`~webclient.models.LoopEvent` for this crawl (loop ``"crawl"``)."""
        from ...kernel.models import LoopEvent

        client = getattr(core, "_client", None)
        bus = getattr(client, "bus", None) if client is not None else None
        if bus is None:
            return
        bus.publish(LoopEvent(
            loop="crawl", phase=cast(Any, phase), round=core._round, detail=detail,
            session_id=core.id or None,
        ))

    async def _fetch_edge(self, core: "Crawl[Any]", edge: Edge) -> Any:
        """Fetch one edge, expand the frontier from its DOM, and return its retained
        projection (``None`` if robots-blocked or the fetch failed -- a
        :class:`Failure` is recorded either way, so the crawl degrades gracefully).
        The audit/resume trail records every edge actually taken."""
        if core.config.obey_robots and not await self._allowed(core, edge.url):
            core.failures.append(
                Failure(url=edge.url, reason="robots-disallowed", depth=edge.depth)
            )
            core._note_error(make("crawl.robots_disallowed", f"robots disallows {edge.url}"), "crawl")
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
            log.warning("crawl: %s failed (%s: %s)", edge.url, type(exc).__name__, exc)
            core.failures.append(
                Failure(url=edge.url, reason=type(exc).__name__, depth=edge.depth)
            )
            core._note_error(
                make("crawl.edge_failed", f"{edge.url}: {type(exc).__name__}: {exc}",
                     cause=getattr(exc, "error", None)),
                "crawl",
            )
            return None
        finally:
            if doc is not None and getattr(doc, "_page", None) is not None:
                await core._client._arelease(doc)

    def _lock(self, core: "Crawl[Any]") -> "asyncio.Lock":
        """The crawl's step lock, created lazily on its running loop (the sync check +
        assign means even the first two concurrent steps share one lock)."""
        if core._step_lock is None:
            core._step_lock = asyncio.Lock()
        return cast("asyncio.Lock", core._step_lock)

    # -- retention ------------------------------------------------------------
    async def _retain(self, core: "Crawl[Any]", doc: "Document") -> Any:
        """Evaluate the crawl's projection expression against the fetched page and keep
        the result -- ``config.project`` is a document-rooted :class:`Expr` (default
        ``doc.card()`` -> a :class:`PageCard`), so ``.pages`` is that expression's
        output. Read while the page is still live (before its browser page is freed)."""
        from ...query.executor import aevaluate

        return await aevaluate(core.config.project, doc, client=core._client)

    # -- frontier selection + scoring -----------------------------------------
    def _select(self, core: "Crawl[Any]", select: Any) -> "list[Edge]":
        """The frontier edges to visit next: a caller-supplied list (adding any brand-new URLs as
        forced edges), else the whole current frontier."""
        if select is not None:
            wanted = [s.url if isinstance(s, Edge) else str(s) for s in select]
            existing = {e.url for e in core.frontier}
            for u in wanted:  # brand-new URLs the caller supplied: add them (forced)
                if u not in existing:
                    self._add_edge(core, u, "", 0, 0.0, force=True)
            want = set(wanted)
            return [e for e in core.frontier if e.url in want]
        if core._driver is None and core.config.order == "manual":
            return []  # pure manual: a bare step() fetches nothing -- the caller selects
        return self._drive_select(core)

    def _drive_select(self, core: "Crawl[Any]") -> "list[Edge]":
        """The auto drive's selection, layered on the manual base: a custom :mod:`.drivers`
        driver if one is set (e.g. an LLM picking the edges most likely to reach a dataset),
        otherwise the built-in best-first heuristic (the top-``width`` frontier edges by
        score). Used by ``run``/``stream`` and a bare ``step()``."""
        from ...loop import Ask

        driver = core._driver
        engine = getattr(core._client, "_the_engine", lambda: None)()
        if driver is None:  # the engine's default crawl driver, if one was installed
            driver = getattr(engine, "drivers", {}).get("crawl") if engine is not None else None
        if driver is not None:
            picked = driver(core)
            if isinstance(picked, Ask):  # a checkpoint: the caller resumes with picks
                core._pending = picked
                if engine is not None:
                    engine.waiting[core.id or f"crawl:{id(core)}"] = core
                self._emit(core, "waiting", ask=picked.model_dump(mode="json"))
                return []
            return cast("list[Edge]", picked)
        ranked = sorted(core.frontier, key=lambda e: self._score(core, e), reverse=True)
        return ranked[: core.config.width]

    def _score(self, core: "Crawl[Any]", edge: Edge) -> float:
        """Best-first ordering: the edge's discovery-time score minus a depth penalty
        (ties break toward shallower pages)."""
        return edge.score - core.config.scoring.depth * edge.depth

    def _link_score(self, core: "Crawl[Any]", text: str, url: str, region: str) -> float:
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
    def _in_scope(self, core: "Crawl[Any]", url: str) -> bool:
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
        self, core: "Crawl[Any]", url: str, text: str, depth: int, score: float = 0.0,
        *, force: bool = False, parent: str = "",
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
        core.frontier.append(Edge(url=url, text=text, depth=depth, score=score, parent=parent))

    def _expand(self, core: "Crawl[Any]", doc: Any, depth: int) -> None:
        """Add ``doc``'s anchor links to the frontier -- dropping page-asset links and
        scoring each by the metric scorer -- then re-sort/cap the frontier."""
        for a in doc.select_all("a[href]"):
            url = str(a.attr("href").url)
            if _ext(_path(url)) in _RESOURCE_EXT:  # a resource link, not a page
                continue
            text = (a.attr("text") or "").strip()
            self._add_edge(core, url, text, depth, self._link_score(core, text, url, a.region), parent=str(getattr(doc, "final_url", "") or getattr(doc, "url", "")))
        self._sort_frontier(core)

    def _expand_xhr(self, core: "Crawl[Any]", doc: Any, depth: int) -> None:
        """Add the data-API endpoints a browser render observed (its XHR/fetch calls)
        to the frontier, so a browser crawl covers the JSON APIs behind the page."""
        from ...kernel.models import NetworkEvent

        for e in doc.events_of(NetworkEvent):
            if getattr(e, "resource_type", None) not in ("xhr", "fetch"):
                continue
            req = e.request
            url = str(req.dispatch("url")) if req is not None else ""
            if url:  # a data-API endpoint -- rides mid-frontier, not sunk as a resource
                self._add_edge(core, url, "[xhr]", depth, 0.5)
        self._sort_frontier(core)

    def _sort_frontier(self, core: "Crawl[Any]") -> None:
        """Keep the frontier best-first (score desc, then shallowest) and hard-capped
        at ``config.max_frontier`` -- a small page budget can still discover hundreds of
        links per page, so the cap bounds memory while keeping the best edges."""
        core.frontier.sort(key=lambda e: (-e.score, e.depth))
        if len(core.frontier) > core.config.max_frontier:
            del core.frontier[core.config.max_frontier :]

    # -- robots.txt (cached per host) -----------------------------------------
    async def _allowed(self, core: "Crawl[Any]", url: str) -> bool:
        """Whether ``url`` is crawlable under its host's robots.txt (fetched and cached once per
        host); allowed when there is no robots file."""
        host = _canon_host(url)
        if host not in core._robots:  # load this host's robots.txt ONCE
            if core._robots_lock is None:  # sync check+assign on the one loop -> racers share it
                core._robots_lock = asyncio.Lock()
            async with core._robots_lock:  # serialise loads; re-check inside so a racer waits, not re-fetches
                if host not in core._robots:
                    core._robots[host] = await self._load_robots(core, url)
        robots: "RobotFileParser | None" = core._robots[host]
        return robots is None or robots.can_fetch("*", url)

    async def _load_robots(self, core: "Crawl[Any]", sample_url: str) -> Any:
        """Fetch and parse a host's robots.txt (derived from ``sample_url``'s origin), returning a
        parser or ``None`` when it is missing/empty."""
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
