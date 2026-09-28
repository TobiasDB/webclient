"""Structural views of a markup :class:`~web.parse.Document`.

``skeleton`` is a token-lean, indented open-tag outline of the DOM: the bloat (scripts / styles /
svg) removed, high-entropy hashed build classes (``css-1a2b3c``) dropped as noise, but every id and
semantic class kept and leaf text hinted -- so an LLM can write CSS selectors for a page cheaply
instead of chewing through raw HTML. ``outline`` is the heading tree (``<h1>``..``<h6>``) for a
table of contents. Both are PURE content structure; a live crawl's XHR / correlation enrichments
belong to a higher layer that annotates this base.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel

from .classes import semantic_classes
from .nodes import Node, classes as _all_classes, tag as _tag

if TYPE_CHECKING:
    from .document import Document

#: subtrees that are noise in a structural outline -- skipped whole.
_SKIP = frozenset({"script", "style", "noscript", "template", "svg", "path", "link", "meta"})


class Heading(BaseModel):
    """One entry in a document's heading outline."""

    level: int   # 1..6
    text: str


def _signature(node: Node) -> str:
    """The open-tag signature shown for a node: ``tag#id.class[.class]`` plus a role/label hint."""
    out = _tag(node)
    node_id = node.get("id")
    if node_id:
        out += f"#{node_id}"
    classes = semantic_classes(_all_classes(node))
    if classes:
        out += "." + ".".join(classes[:4])
    for a in ("role", "aria-label", "name", "type", "placeholder"):
        v = node.get(a)
        if v:
            out += f" [{a}={v[:24]}]"
            break
    return out


def skeleton(doc: "Document", *, max_lines: int = 400, text_chars: int = 40, max_depth: int = 30) -> str:
    """A token-lean indented open-tag outline of the DOM (ids + semantic classes kept, hashed build
    classes + script/style/svg dropped, leaf text hinted to ``text_chars``). Empty for non-markup."""
    if not doc._markup():
        return ""
    lines: list[str] = []
    _emit(doc._root(), 0, lines, max_lines=max_lines, text_chars=text_chars, max_depth=max_depth)
    return "\n".join(lines[:max_lines])


def _emit(node: Node, depth: int, lines: list[str], *, max_lines: int, text_chars: int, max_depth: int) -> None:
    if len(lines) >= max_lines or depth > max_depth or _tag(node) in _SKIP:
        return
    indent = "  " * depth
    line = indent + _signature(node)
    own = " ".join((node.text or "").split())
    kids = [c for c in node if _tag(c) not in _SKIP]
    if not kids and own:  # a leaf: hint its text
        line += f"  {own[:text_chars]!r}"
    elif own and len(own) > 1:
        line += f"  {own[:text_chars]!r}"
    lines.append(line)
    for child in kids:
        _emit(child, depth + 1, lines, max_lines=max_lines, text_chars=text_chars, max_depth=max_depth)


def outline(doc: "Document") -> "list[Heading]":
    """The document's heading tree (``<h1>``..``<h6>``) in order -- a table of contents."""
    if not doc._markup():
        return []
    out: list[Heading] = []
    for el in doc.select_all("h1, h2, h3, h4, h5, h6"):
        tag = _tag(el._node)
        out.append(Heading(level=int(tag[1]), text=el.text))
    return out


__all__ = ["skeleton", "outline", "Heading"]
