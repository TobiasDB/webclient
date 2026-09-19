"""Crawl: a stateful, scoped site traversal -- the client holds it, like a session.

Created by ``WebClient.crawl(seeds, ...)`` (and ``sitemap(url)``). Drive it two ways
over the one step-engine::

    with wc.crawl("https://site", keywords=["pricing"]) as crawl:
        crawl.run()                     # batch: drive to completion, read .pages
        # or manual:  crawl.step(picks) / crawl.step([new_url])   (one round)

The client MANAGES the frontier (dedup, scope, fetching); ``config.order`` decides
whether a bare round self-drives best-first or waits for the caller's selection.
State + config come from the ``ICrawl`` model (:mod:`.models`); behaviour is the
:class:`~.backing.CrawlBacking`. ``.pages`` holds a lean :class:`PageCard` per page by
default (``config.retain="document"`` keeps the whole Document).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, AsyncIterator, ClassVar, Iterator, cast

from pydantic import PrivateAttr

from ...collection import Collection
from ...query.expr import Expr
from ...query.plan import Plan
from ..session_core import SessionCore
from ..web_core import Backing
from .models import CrawlConfig, CrawlState, Edge, Failure, ICrawl, PageCard  # noqa: F401  (re-exported)

from .backing import CrawlBacking

if TYPE_CHECKING:
    from ..client import WebClient


class _CrawlLazy:
    """The crawl's lazy views. ``frontier`` returns the pending edges as a
    ``Collection[Edge]`` (filterable / projectable / iterable) rather than the eager
    ``list[Edge]`` -- a snapshot at access time, so re-read it after a step."""

    __slots__ = ("_crawl",)

    def __init__(self, crawl: "Crawl") -> None:
        self._crawl = crawl

    @property
    def frontier(self) -> "Collection[Edge]":
        return Collection(list(self._crawl.frontier), client=self._crawl._client)


class Crawl(SessionCore, ICrawl):
    """A scoped site traversal. State (frontier / pages / config) is the ``ICrawl``
    model it inherits; this core adds the client binding, the dedup/robots machinery,
    and ``state()`` (a resumable snapshot). A context manager; its ops (``step`` /
    ``run`` / ``done``) are the ``CrawlBacking``. Under a remote client a crawl is
    data-producing, not a server-side object: ``run``/``stream`` execute the whole crawl
    as ONE plan (``WebClient.crawl(seeds, ...).run().pages``) server-side over
    ``/execute`` -- its config is fully serializable -- and the pages ride back. Interactive
    ``step`` (a live, mutating frontier) has no stateless plan form, so it stays local."""

    _client: "WebClient" = PrivateAttr(default=None)  # type: ignore[assignment]
    _store: dict[str, Any] = PrivateAttr(default_factory=dict)  # SessionCore.store (unused for now)
    _seen: set[str] = PrivateAttr(default_factory=set)  # dedup ledger (canonical urls)
    _robots: dict[str, Any] = PrivateAttr(default_factory=dict)  # per-host RobotFileParser
    #: serialises ``step`` rounds so a step's frontier-claim + page-budget + expansion
    #: is atomic. Without it, concurrently-awaited steps (async mode) each read the
    #: same ``len(pages)`` before appending, so each claims the full remaining budget
    #: and ``max_pages`` is blown past. Lazily created on the crawl's own loop.
    _step_lock: Any = PrivateAttr(default=None)  # asyncio.Lock (lazy, loop-bound)
    #: pages claimed by a round but not yet appended -- reserved under the step lock so the
    #: page budget stays correct while a round fetches its claimed edges CONCURRENTLY (outside
    #: the lock), and concurrent rounds don't both claim the same remaining budget.
    _inflight: int = PrivateAttr(default=0)

    BACKINGS: ClassVar[tuple[Backing, ...]] = (CrawlBacking(),)

    def _remote_call(self, op: str, is_prop: bool) -> Any:
        """Under a remote client a crawl is DATA-PRODUCING, not a stateful server object:
        ``run`` executes ``WebClient.crawl(seeds, ...).run().pages`` as one plan over
        ``/execute`` (the config is fully serializable) and the returned pages populate
        this handle. Interactive ``step`` hands back a live, mutating frontier -- it has no
        stateless plan form, the exact analogue of an OPEN ``.step(...)`` sequence -- so it
        stays engine-local; run/stream remotely instead."""
        if op == "run":
            def _run(*a: Any, **k: Any) -> "Crawl":
                self.pages = list(self._remote_expr().run().pages.collect())
                self.status = "closed"  # a remote run is one-shot and complete
                return self
            return _run
        if op == "step":
            def _step(*a: Any, **k: Any) -> "Crawl":
                raise NotImplementedError(
                    "a remote crawl runs as one plan (its frontier lives server-side); "
                    "interactive step() runs only on a local client -- use run() or "
                    "stream() to crawl remotely"
                )
            return _step
        return super()._remote_call(op, is_prop)

    def _remote_expr(self) -> Any:
        """The ``WebClient.crawl(...)`` plan that rebuilds THIS crawl server-side: its seed
        URLs plus its config as keyword args (``project`` as a serialized document plan the
        server rebuilds, ``resolve`` as its dict). Rooted at the remote client, so
        ``.run().pages`` collects the crawl's pages over ``/execute``."""
        c = self.config
        root = Expr(Plan(root="WebClient"), self._client)
        return root.crawl(
            [e.url for e in self.frontier],
            auto=c.order == "best-first",
            width=c.width, depth=c.max_depth, max_pages=c.max_pages,
            max_frontier=c.max_frontier, same_origin=c.same_origin,
            allow_subdomains=c.allow_subdomains, allow_domains=c.allow_domains,
            deny_domains=c.deny_domains, allow_countries=c.allow_countries,
            deny_countries=c.deny_countries, include=c.include, exclude=c.exclude,
            include_xhr=c.include_xhr, keywords=c.keywords, obey_robots=c.obey_robots,
            browser=c.browser, resolve=c.resolve.model_dump() if c.resolve else None,
            project=c.project._plan.model_dump(),
        )

    def bind(self, client: "WebClient") -> "Crawl":
        """Share ``client``'s engine (its ``afetch``/pool drive the crawl) and seed
        the dedup ledger (canonicalised) from the initial frontier."""
        from .canon import _canon

        self._client = client
        # dedup the seed frontier itself by canonical key (not just the ledger), so
        # two seeds that collapse to one target aren't both fetched.
        seen: set[str] = set()
        deduped = []
        for edge in self.frontier:
            key = _canon(edge.url)
            if key not in seen:
                seen.add(key)
                deduped.append(edge)
        self.frontier = deduped
        self._seen = seen
        return self

    def state(self) -> CrawlState:
        """A resumable snapshot: config + the unresolved frontier + the seen ledger +
        the edges taken. Pass it back as ``wc.crawl(seeds, resume=state)`` to continue."""
        return CrawlState(
            config=self.config, scope=self.scope, frontier=list(self.frontier),
            seen=sorted(self._seen), history=list(self.history),
            failures=list(self.failures),
        )

    # -- streaming: the same engine as run(), consumed incrementally -----------
    def stream(self) -> "Iterator[Any]":
        """Stream the crawl: drive best-first and yield each page's retained projection
        as it is fetched (``for card in crawl.stream()``). Breaking pauses the crawl --
        the frontier + seen ledger stay intact, so re-entering the stream (or calling
        ``run()``) continues. ``run()`` is this stream drained; ``list(crawl.stream())``
        its pages. (Not ``__iter__`` -- iterating a pydantic model yields its fields.)"""
        if self._dispatch_mode() == "remote":
            return self._remote_stream()
        backing = cast(CrawlBacking, self.BACKINGS[0])
        return self._client.loop().stream(backing._astream(self))

    def _remote_stream(self) -> "Iterator[Any]":
        """Remote streaming: a remote crawl is data-producing (its frontier lives inside
        one server-side ``/execute``), so there is no per-round handshake to pause on --
        run the crawl as one plan and yield its pages. (True incremental streaming needs a
        local crawl.)"""
        self.dispatch("run")  # one-shot server-side crawl; populates self.pages
        yield from self.pages

    def astream(self) -> "AsyncIterator[Any]":
        """The async twin of :meth:`stream`: ``async for card in crawl.astream()``. Same
        best-first engine and pause/resume semantics, delivered on the caller's loop. A
        remote crawl is data-producing -- run it as one plan and hand back its pages."""
        if self._dispatch_mode() == "remote":
            async def _aiter() -> "AsyncIterator[Any]":
                self.dispatch("run")
                for page in self.pages:
                    yield page
            return _aiter()
        backing = cast(CrawlBacking, self.BACKINGS[0])
        return self._client.loop().astream(backing._astream(self))

    @property
    def lazy(self) -> "_CrawlLazy":
        """The crawl's lazy views (currently ``lazy.frontier`` -> ``Collection[Edge]``)."""
        return _CrawlLazy(self)


__all__ = ["Crawl", "Edge", "Failure", "PageCard", "CrawlConfig", "CrawlState", "ICrawl"]
