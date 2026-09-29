"""web.crawl's data models -- what to crawl (:class:`Goal`) and what it reports (:class:`CrawlEvent`).

Pure data + the small traversal-scope predicates a Goal is configured with. The crawl behaviour
(the :class:`~web.crawl.Crawler` breadth-first walk) lives in the package ``__init__``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from pydantic import BaseModel
from web.parse import Document

if TYPE_CHECKING:  # only for Goal's annotation -- frontier.py imports FrontierItem from here
    from .frontier import FrontierMiddleware


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
    ``text`` (the anchor text of the link), ``depth`` (link-distance from a seed; seeds are 0), and
    -- from the (already-fetched) page it was found on -- ``parent`` (its URL), ``parent_status``
    (its HTTP status), ``parent_title`` (its ``<title>`` / description), and ``parent_flags`` (the
    detection flags that fired on it). Seeds carry only ``url``."""

    url: str
    depth: int = 0
    parent: str = ""
    text: str = ""
    parent_status: int = 0
    parent_title: str = ""
    parent_flags: "list[str]" = field(default_factory=list)


def same_origin(doc: Document, link: str) -> bool:
    """Default scope: stay on the document's host."""
    return urlparse(link).hostname == urlparse(doc.url).hostname


@dataclass
class Goal:
    """What to crawl. ``start`` is the entry point(s); ``scope`` decides which links to follow;
    ``collect`` (optional) decides which resolved documents are RESULTS (default: all of them);
    ``max_pages`` bounds how many pages are fetched. ``frontier`` is a chain of turn-based
    :data:`~web.crawl.frontier.FrontierMiddleware` that picks which pending URLs to expand next (an
    LLM / a heuristic; default = empty = breadth-first). ``assess`` computes each page's flags for
    its :class:`CrawlEvent` (observability; costs a ``flags()`` per page). ``sitemap`` also seeds
    from the site's sitemap(s); ``respect_robots`` honours robots.txt (and seeds from its
    ``Sitemap:`` lines). Seeds/frontier are derived from this."""

    start: "str | list[str]"
    scope: Follow = same_origin
    collect: "Collect | None" = None
    max_pages: int = 15
    frontier: "tuple[FrontierMiddleware, ...]" = ()
    assess: bool = False
    sitemap: bool = False
    respect_robots: bool = False


__all__ = ["Goal", "CrawlEvent", "Follow", "Collect", "FrontierItem", "same_origin"]
