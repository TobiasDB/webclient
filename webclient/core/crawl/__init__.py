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
    _seen: set[str] = PrivateAttr(default_factory=set)  # dedup ledger
    _robots: Any = PrivateAttr(default=None)  # cached RobotFileParser
    _robots_loaded: bool = PrivateAttr(default=False)

    BACKINGS: ClassVar[tuple[Backing, ...]] = (CrawlBacking(),)

    def bind(self, client: "WebClient") -> "Crawl":
        """Share ``client``'s engine (its ``afetch``/pool drive the crawl) and seed
        the dedup ledger from the initial frontier."""
        self._client = client
        self._seen = {e.url for e in self.frontier}
        return self


__all__ = ["Crawl", "Edge", "ICrawl"]
