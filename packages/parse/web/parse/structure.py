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
from .index import interactive as _interactive
from .nodes import Node, classes as _all_classes, tag as _tag
from .records import scan as _scan

if TYPE_CHECKING:
    from .document import Document

#: subtrees that are noise in a structural outline -- skipped whole.
_SKIP = frozenset({"script", "style", "noscript", "template", "svg", "path", "link", "meta"})
#: page-chrome landmarks dropped when ``drop_chrome`` (so records aren't buried under menus).
_CHROME = frozenset({"nav", "header", "footer", "aside"})
#: native controls whose interactivity is obvious from the tag -- not worth a ``← clickable`` mark.
_OBVIOUS = frozenset({"a", "button", "input", "select", "textarea", "summary", "label", "option"})


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


def skeleton(doc: "Document", *, max_lines: int = 400, text_chars: int = 40, max_depth: int = 30,
             mark_records: bool = True, mark_interactive: bool = True, drop_chrome: bool = False) -> str:
    """A token-lean indented open-tag outline of the DOM (ids + semantic classes kept, hashed build
    classes + script/style/svg dropped, leaf text hinted to ``text_chars``). Empty for non-markup.

    Enrichments (each on by default, each a no-op when its data is absent): ``mark_records`` flags
    the repeating dataset region in place (``← RECORD LIST · N · select_all("li.item")``);
    ``mark_interactive`` flags NON-obvious controls (a div/span made clickable via role/onclick/
    tabindex) ``← clickable``; ``drop_chrome`` omits nav/header/footer/aside so a big page's records
    aren't buried under menus."""
    if not doc._markup():
        return ""
    marks: dict[int, str] = {}
    if mark_records:
        for node, region in _scan(doc)[:2]:
            marks[id(node)] = f'  ← RECORD LIST · {region.count} · select_all("{region.item_selector}")'
    lines: list[str] = []
    _emit(doc._root(), 0, lines, marks=marks, mark_interactive=mark_interactive, drop_chrome=drop_chrome,
          max_lines=max_lines, text_chars=text_chars, max_depth=max_depth)
    return "\n".join(lines[:max_lines])


def _emit(node: Node, depth: int, lines: list[str], *, marks: dict[int, str], mark_interactive: bool,
          drop_chrome: bool, max_lines: int, text_chars: int, max_depth: int) -> None:
    tag = _tag(node)
    if len(lines) >= max_lines or depth > max_depth or tag in _SKIP or (drop_chrome and tag in _CHROME):
        return
    line = "  " * depth + _signature(node)
    own = " ".join((node.text or "").split())
    kids = [c for c in node if _tag(c) not in _SKIP]
    if own and (not kids or len(own) > 1):  # hint a leaf's (or a short container's) own text
        line += f"  {own[:text_chars]!r}"
    if mark_interactive and tag not in _OBVIOUS and _interactive(node):
        line += "  ← clickable"
    line += marks.get(id(node), "")  # a record-list container marker, if this is one
    lines.append(line)
    for child in kids:
        _emit(child, depth + 1, lines, marks=marks, mark_interactive=mark_interactive,
              drop_chrome=drop_chrome, max_lines=max_lines, text_chars=text_chars, max_depth=max_depth)


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
