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
from ..web_core import Backing, WebCore
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


class Crawl(WebCore, ICrawl):
    """A scoped site traversal. State (frontier / pages / config) is the ``ICrawl``
    model it inherits; this core adds the client binding, the dedup/robots machinery,
    and ``state()`` (a resumable snapshot). A context manager; its ops (``step`` /
    ``run`` / ``done``) are the ``CrawlBacking``. Under a remote client it is a thin
    handle over a server-side crawl: ``step``/``run`` dispatch to the server (which
    owns the frontier/fetch) and refresh this handle's mirrored state, so turn-based
    stepping works remotely too."""

    _client: "WebClient" = PrivateAttr(default=None)  # type: ignore[assignment]
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
    #: set on a remote handle -- the id of the server-side crawl this mirrors, so
    #: ``step``/``run`` round-trip to it (empty on a local crawl).
    _crawl_id: str = PrivateAttr(default="")

    BACKINGS: ClassVar[tuple[Backing, ...]] = (CrawlBacking(),)

    def _remote_call(self, op: str, is_prop: bool) -> Any:
        """A remote crawl's ``step``/``run`` advance the server-side crawl and refresh
        this handle's mirror (the frontier/pages are then read locally off the mirror --
        only the IO ops round-trip). Every other op runs on the local mirror, so
        ``done``/``pages``/``frontier`` need no round-trip."""
        if op in ("step", "run"):
            client = cast(Any, self._client)  # a RemoteWebClientCore in remote mode
            return lambda *a, **k: client._advance_crawl(self, op, *a, **k)
        return super()._remote_call(op, is_prop)

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
        """Remote streaming: drive the server-side crawl one ``step`` round-trip at a
        time, yielding each round's new pages off the refreshed mirror. Break pauses
        exactly as locally (the server keeps the frontier)."""
        seen = len(self.pages)  # only yield pages fetched during THIS stream
        while not self.done:
            self.dispatch("step")
            fresh = self.pages[seen:]
            seen = len(self.pages)
            if not fresh:  # a round that fetched nothing (all blocked) -- stop
                break
            yield from fresh

    def astream(self) -> "AsyncIterator[Any]":
        """The async twin of :meth:`stream`: ``async for card in crawl.astream()``. Same
        best-first engine and pause/resume semantics, delivered on the caller's loop."""
        backing = cast(CrawlBacking, self.BACKINGS[0])
        return self._client.loop().astream(backing._astream(self))

    @property
    def lazy(self) -> "_CrawlLazy":
        """The crawl's lazy views (currently ``lazy.frontier`` -> ``Collection[Edge]``)."""
        return _CrawlLazy(self)


__all__ = ["Crawl", "Edge", "Failure", "PageCard", "CrawlConfig", "CrawlState", "ICrawl"]
