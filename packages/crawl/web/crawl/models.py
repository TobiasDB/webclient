"""web.crawl's data models -- what to crawl (:class:`Goal`) and what it reports (:class:`CrawlEvent`).

Pure data + the small traversal-scope predicates a Goal is configured with. The crawl behaviour
(the :class:`~web.crawl.Crawler` breadth-first walk) lives in the package ``__init__``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from urllib.parse import urlparse

from pydantic import BaseModel
from web.parse import Document


class CrawlEvent(BaseModel):
    """One page the crawl fetched -- what it was, its HTTP ``status`` and whether it was ``ok`` (a
    2xx), and (when the goal ``assess``es) the ``flags`` the detection surface fired on it, plus how
    many pages have been fetched so far. Enough for a caller to SEE the crawl happening -- e.g. that
    a picked page is actually a ``blocked`` bot-wall, or a 404."""

    topic: str = "crawl"
    url: str = ""
    fetched: int = 0
    status: int = 0
    ok: bool = False
    flags: list[str] = []


#: whether to FOLLOW ``link`` found on ``doc`` -- the crawl's traversal scope.
Follow = Callable[[Document, str], bool]
#: whether a resolved ``doc`` is a RESULT to yield (vs only traversed for its links).
Collect = Callable[[Document], bool]


@dataclass
class FrontierItem:
    """A pending URL on the crawl frontier, with the context a policy needs to prioritise it:
    ``depth`` (link-distance from a seed; seeds are 0) and ``parent`` (the page it was found on)."""

    url: str
    depth: int = 0
    parent: str = ""


#: a TURN-BASED frontier policy: given the pending frontier, return the URLs to expand next in
#: priority order -- a returned SUBSET prunes the rest, ``[]`` stops the crawl. Async, so an LLM can
#: score / choose which links to follow each turn. Default (``None``) is breadth-first (FIFO).
Frontier = Callable[[Sequence["FrontierItem"]], Awaitable[Sequence[str]]]


def by_score(score: "Callable[[FrontierItem], float]") -> Frontier:
    """Turn a per-item scorer into a :data:`Frontier` policy -- expand highest-score first (a cheap,
    synchronous alternative to an LLM policy)."""

    async def policy(items: "Sequence[FrontierItem]") -> "Sequence[str]":
        return [it.url for it in sorted(items, key=lambda i: -score(i))]

    return policy


def same_origin(doc: Document, link: str) -> bool:
    """Default scope: stay on the document's host."""
    return urlparse(link).hostname == urlparse(doc.url).hostname


@dataclass
class Goal:
    """What to crawl. ``start`` is the entry point(s); ``scope`` decides which links to follow;
    ``collect`` (optional) decides which resolved documents are RESULTS (default: all of them);
    ``max_pages`` bounds how many pages are fetched. ``frontier`` is a turn-based policy that picks
    which pending URLs to expand next (an LLM / heuristic; default breadth-first FIFO). ``assess``
    computes each page's flags for its :class:`CrawlEvent` (observability; costs a ``flags()`` per
    page). ``sitemap`` also seeds from the site's sitemap(s); ``respect_robots`` honours robots.txt
    (and seeds from its ``Sitemap:`` lines). Seeds/frontier are derived from this."""

    start: "str | list[str]"
    scope: Follow = same_origin
    collect: "Collect | None" = None
    max_pages: int = 50
    frontier: "Frontier | None" = None
    assess: bool = False
    sitemap: bool = False
    respect_robots: bool = False


__all__ = [
    "Goal",
    "CrawlEvent",
    "Follow",
    "Collect",
    "Frontier",
    "FrontierItem",
    "by_score",
    "same_origin",
]
