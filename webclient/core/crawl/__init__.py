"""Crawl: a stateful, scoped site traversal -- the client holds it, like a session.

Created by ``WebClient.crawl(seeds, ...)`` (and ``sitemap(url)``), used as a
context manager::

    with wc.crawl("https://site", keywords=["pricing"]) as crawl:
        while not crawl.done and len(crawl.pages) < 20:
            picks = [e for e in crawl.frontier if "docs" in e.url]  # LLM/user steers
            crawl.step(picks)                                        # client fetches

The client MANAGES the frontier (dedup, scope, fetching) but DEFERS the round
selection to the caller; ``auto=True`` self-drives best-first instead. Its Core
Fields + ops come from the ``ICrawl`` model/interface it inherits (:mod:`.models`);
behaviour is the :class:`~.backing.CrawlBacking`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

from pydantic import PrivateAttr

from ..web_core import Backing, WebCore
from .backing import CrawlBacking
from .models import Edge, ICrawl  # noqa: F401  (Edge re-exported)

if TYPE_CHECKING:
    from ..client import WebClient


class Crawl(WebCore, ICrawl):
    """A scoped site traversal. State (frontier / pages / options) is the
    ``ICrawl`` model it inherits; this core adds the client binding and the
    dedup/robots machinery. A context manager (``with wc.crawl(...) as crawl``);
    its ops (``step`` / ``run`` / ``done``) are the ``CrawlBacking``."""

    _client: "WebClient" = PrivateAttr(default=None)  # type: ignore[assignment]
    _seen: set[str] = PrivateAttr(default_factory=set)  # dedup ledger (canonical urls)
    _robots: dict[str, Any] = PrivateAttr(default_factory=dict)  # per-host RobotFileParser

    BACKINGS: ClassVar[tuple[Backing, ...]] = (CrawlBacking(),)

    def bind(self, client: "WebClient") -> "Crawl":
        """Share ``client``'s engine (its ``afetch``/pool drive the crawl) and seed
        the dedup ledger (canonicalised) from the initial frontier."""
        from .backing import _canon

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


__all__ = ["Crawl", "Edge", "ICrawl"]
