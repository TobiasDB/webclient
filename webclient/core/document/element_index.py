"""ElementIndexBacking: the indexed element table facet over :mod:`webclient.dom.index`.

The pure half (``index_elements`` / ``durable_selector`` / ``record_options`` /
``field_options``) lives in :mod:`webclient.dom.index` and is re-exported here; this backing
only binds it to a document's cached tree.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ...dom.index import (  # noqa: F401  (re-exported)
    IndexedElement,
    durable_selector,
    field_options,
    index_elements,
    record_options,
    render_table as _render_table,
)
from ..web_core import Backing
from .html import tree

if TYPE_CHECKING:
    from . import Document


class ElementIndexBacking(Backing):
    """The indexed element table facet: ``controls()`` (interactive elements) /
    ``content_elements()`` (text/record elements) as :class:`IndexedElement` lists, and
    ``element_table()`` rendering
    the numbered view an agent picks indexes from. html/xml only."""

    provides = frozenset({"controls", "content_elements", "element_table"})
    gate = "ok"

    def applies(self, core: "Document") -> bool:
        """In play for markup documents (html/xml) -- the only kinds with an addressable DOM."""
        return core.kind in ("html", "xml")

    def controls(self, core: "Document") -> "list[IndexedElement]":
        """The INTERACTIVE elements (buttons / fields / links / …) as a numbered, class-free
        table -- what an interaction agent picks a target from; each carries a durable selector."""
        return index_elements(tree(core), kind="interactive")

    def content_elements(self, core: "Document") -> "list[IndexedElement]":
        """The text-bearing / repeated-record elements as a numbered table -- what a query agent
        picks a record + fields from; ``repeats`` marks a member of a repeated row."""
        return index_elements(tree(core), kind="content")

    def element_table(self, core: "Document", *, interactive: bool = True) -> str:
        """The numbered element table as text (interactive controls by default, else content) --
        the token-lean view an agent reasons over, returning indexes we resolve to selectors."""
        rows = self.controls(core) if interactive else self.content_elements(core)
        return _render_table(rows)


__all__ = [
    "ElementIndexBacking", "durable_selector", "index_elements",
    "record_options", "field_options",
]
