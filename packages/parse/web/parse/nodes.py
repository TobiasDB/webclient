"""Low-level lxml node helpers -- the ONE place the parse layer touches raw tree nodes.

Every other parse module (document, content, records, structure, index) reads nodes through these,
so the tag/text primitives and the node type live in a single place instead of being re-derived per
module. ``Node`` is the lxml element type; keep it here so a stub gap is fixed once.
"""

from __future__ import annotations

from lxml.etree import _Element as Node


def tag(node: Node) -> str:
    """The element's lowercased tag name, or ``""`` for a comment/PI/non-string tag."""
    t = node.tag
    return t.lower() if isinstance(t, str) else ""


def text(node: Node) -> str:
    """All descendant text of ``node``, whitespace-collapsed (works for HTML and XML nodes)."""
    parts = [t.decode() if isinstance(t, bytes) else t for t in node.itertext()]
    return " ".join("".join(parts).split())


def classes(node: Node) -> "list[str]":
    """The element's ``class`` tokens (all of them; filtering is :mod:`.classes`' job)."""
    value = node.get("class")
    return value.split() if value else []


def query(node: Node, css: str) -> "list[Node]":
    """Every descendant matching a CSS selector -- the one place the ``cssselect`` call lives."""
    return node.cssselect(css)


__all__ = ["Node", "tag", "text", "classes", "query"]
