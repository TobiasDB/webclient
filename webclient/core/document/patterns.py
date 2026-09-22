"""PatternsBacking: the ``patterns`` facet -- the typed Document surface over the pattern
registry in :mod:`webclient.patterns` (thin: build the context from the document, ask the
registry)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ...patterns import PatternContext, PatternHint, detect
from ..web_core import Backing
from .html import tree

if TYPE_CHECKING:
    from . import Document


class PatternsBacking(Backing):
    """The ``patterns`` facet: recurring-structure hints for extraction / interaction /
    crawl (html/xml only)."""

    provides = frozenset({"patterns"})
    gate = "ok"

    def applies(self, core: "Document") -> bool:
        """In play for markup documents (the only kinds with a DOM to find patterns in)."""
        return core.kind in ("html", "xml")

    def patterns(self, core: "Document", *, for_: "str | None" = None) -> "list[PatternHint]":
        """The page's detected patterns, most confident first: the repeating record list(s)
        to ``select_all`` (``for_="extract"``), repeated controls to act on per item
        (``"interact"``), and the page-template signature (``"crawl"``)."""
        ctx = PatternContext(tree=tree(core), url=core.final_url or core.url, events=list(core._events))
        return detect(ctx, for_=for_)  # type: ignore[arg-type]


__all__ = ["PatternsBacking"]
