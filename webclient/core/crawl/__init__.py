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

from typing import TYPE_CHECKING, Any, ClassVar

from pydantic import PrivateAttr

from ..web_core import Backing, WebCore
from .models import CrawlConfig, CrawlState, Edge, ICrawl, PageCard  # noqa: F401  (re-exported)

from .backing import CrawlBacking

if TYPE_CHECKING:
    from ..client import WebClient


class Crawl(WebCore, ICrawl):
    """A scoped site traversal. State (frontier / pages / config) is the ``ICrawl``
    model it inherits; this core adds the client binding, the dedup/robots machinery,
    and ``state()`` (a resumable snapshot). A context manager; its ops (``step`` /
    ``run`` / ``done``) are the ``CrawlBacking``."""

    _client: "WebClient" = PrivateAttr(default=None)  # type: ignore[assignment]
    _seen: set[str] = PrivateAttr(default_factory=set)  # dedup ledger (canonical urls)
    _robots: dict[str, Any] = PrivateAttr(default_factory=dict)  # per-host RobotFileParser
    #: serialises ``step`` rounds so a step's frontier-claim + page-budget + expansion
    #: is atomic. Without it, concurrently-awaited steps (async mode) each read the
    #: same ``len(pages)`` before appending, so each claims the full remaining budget
    #: and ``max_pages`` is blown past. Lazily created on the crawl's own loop.
    _step_lock: Any = PrivateAttr(default=None)  # asyncio.Lock (lazy, loop-bound)

    BACKINGS: ClassVar[tuple[Backing, ...]] = (CrawlBacking(),)

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
        )


__all__ = ["Crawl", "Edge", "PageCard", "CrawlConfig", "CrawlState", "ICrawl"]
