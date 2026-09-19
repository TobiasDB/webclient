"""Crawl drivers: auto edge-selection policies layered on the ``manual`` base.

A crawl's BASE is manual -- a frontier plus ``step(picks)`` and frontier exposure (see
:class:`~webclient.core.crawl.backing.CrawlBacking`). A *driver* is the auto policy on
top: given the crawl, it returns the frontier edges to expand this round; ``run`` /
``stream`` / a bare ``step()`` consult it. Two come built in and need no object:

- **best-first** (the default) -- the heuristic scorer in the backing, used when no driver
  is set and the crawl is not ``manual``.
- **manual** -- no driver set AND ``order="manual"``: a bare ``step()`` fetches nothing;
  the caller drives with explicit ``step(picks)``.

A custom driver (e.g. an LLM that picks the edges most likely to reach a dataset) is any
``Callable[[Crawl], list[Edge]]`` passed as ``wc.crawl(..., driver=...)``. :func:`from_picks`
adapts a "hand me the frontier, I'll return the URLs to expand" function (like the
onboarding pipeline's model pick) into one, so the pick logic lives in the caller and the
driving (list frontier -> pick -> expand, round by round) lives in the crawl.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from . import Crawl
    from .models import Edge

#: an auto edge-selection policy: given the crawl, the frontier edges to fetch this round.
Driver = Callable[["Crawl"], "list[Edge]"]


def from_picks(pick: "Callable[[list[Edge]], Sequence[str]]") -> Driver:
    """Adapt a pick-URLs function into a :data:`Driver`. Each round the crawl hands ``pick``
    its current frontier edges; ``pick`` returns the URLs to expand (a subset), and this
    maps them back to the frontier edges to fetch. So an LLM (or any) selector supplies only
    the policy -- ``list[Edge] -> list[url]`` -- while the crawl owns the driving loop."""

    def _driver(crawl: "Crawl") -> "list[Edge]":
        chosen = set(pick(list(crawl.frontier)))
        return [e for e in crawl.frontier if e.url in chosen]

    return _driver


__all__ = ["Driver", "from_picks"]
