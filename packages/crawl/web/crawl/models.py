"""web.crawl's data models -- what to crawl (:class:`Goal`) and what it reports (:class:`CrawlEvent`).

Pure data + the small traversal-scope predicates a Goal is configured with. The crawl behaviour
(the :class:`~web.crawl.Crawler` breadth-first walk) lives in the package ``__init__``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlparse

from pydantic import BaseModel
from web.parse import Document


class CrawlEvent(BaseModel):
    """One page the crawl fetched, with how many it has fetched so far."""

    topic: str = "crawl"
    url: str = ""
    fetched: int = 0


#: whether to FOLLOW ``link`` found on ``doc`` -- the crawl's traversal scope.
Follow = Callable[[Document, str], bool]
#: whether a resolved ``doc`` is a RESULT to yield (vs only traversed for its links).
Collect = Callable[[Document], bool]


def same_origin(doc: Document, link: str) -> bool:
    """Default scope: stay on the document's host."""
    return urlparse(link).hostname == urlparse(doc.url).hostname


@dataclass
class Goal:
    """What to crawl. ``start`` is the entry point(s); ``scope`` decides which links to follow;
    ``collect`` (optional) decides which resolved documents are RESULTS (default: all of them);
    ``max_pages`` bounds how many pages are fetched. ``sitemap`` also seeds the frontier from the
    site's sitemap(s); ``respect_robots`` honours robots.txt (and seeds from its ``Sitemap:``
    lines). Seeds/frontier are derived from this."""

    start: "str | list[str]"
    scope: Follow = same_origin
    collect: "Collect | None" = None
    max_pages: int = 50
    sitemap: bool = False
    respect_robots: bool = False


__all__ = ["Goal", "CrawlEvent", "Follow", "Collect", "same_origin"]
