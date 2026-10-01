"""Record-region detection -- find the dominant repeating structure, i.e. the DATASET.

Most "0 rows extracted" failures are picking the wrong container to ``select_all``. This locates
the container whose children form the largest run of structurally-identical siblings and proposes
a concrete item selector -- so "where is the list?" is answered mechanically. Pure and static (over
the parsed lxml tree); the query/onboarding "Locate" step stands on this.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel

from .classes import class_hook
from .nodes import Node
from .nodes import classes as _all_classes
from .nodes import tag as _tag

if TYPE_CHECKING:
    from .document import Document

_SKIP = frozenset({"script", "style", "noscript", "template", "svg", "path", "br", "hr"})
_DRAWING = frozenset({"svg", "math", "canvas"})  # repetition here is geometry, not records
#: form CONTROLS: a <select>'s options / a <datalist> repeat, but they are choices, never records;
#: the <head> repeats <meta> / <link> tags that are not content at all.
_CONTROLS = frozenset({"select", "datalist", "optgroup", "head"})
_CHROME_TAGS = frozenset({"nav", "header", "footer", "aside"})
_CHROME_ROLES = frozenset({"navigation", "banner", "contentinfo", "complementary"})
_WRAPPER_TAGS = frozenset({"div", "span", "li", "section", "article"})


class RecordRegion(BaseModel):
    """A detected repeating region -- a dataset candidate. ``item_selector`` is a suggested
    ``select_all`` target; ``score`` = count x richness x chrome-penalty (higher = more dataset-like).
    """

    item_selector: str
    #: the CONTAINER's own durable selector (tag + its class hooks / id) -- ``container_selector
    #: item_selector`` scopes the items when the bare item selector also matches elsewhere on the
    #: page (a nav's ``li[class*="ListItem"]`` next to the news list's).
    container_selector: str = ""
    count: int
    container_tag: str = ""
    container_id: str = ""
    score: float = 0.0


def _classes(el: Node) -> "list[str]":
    """The DURABLE class hooks of an element (``.item`` / ``[class*="StyledListItem"]``), in class
    order -- a generated class contributes its stable stem, noise contributes nothing. These are
    the record's class IDENTITY (its signature, its selector); a labelled build class counts."""
    return [h for c in _all_classes(el) if (h := class_hook(c))]


def _kids(el: Node) -> "list[Node]":
    return [c for c in el if _tag(c) and _tag(c) not in _SKIP]


def _unwrap(el: Node) -> Node:
    """Descend through anonymous single-child wrappers (the SPA/Tailwind per-record ``<div>``) to
    the semantic record inside, so records group by their inner shape, not a bare wrapper tag.
    """
    for _ in range(4):
        if _tag(el) not in _WRAPPER_TAGS or _classes(el):
            break
        kids = _kids(el)
        if len(kids) != 1:
            break
        el = kids[0]
    return el


def _sig(el: Node) -> "tuple[str, frozenset[str], tuple[str, ...]]":
    """A light structural signature of the UNWRAPPED record: tag + semantic classes + child shape.
    Two siblings share a signature when they are the same KIND of record."""
    el = _unwrap(el)
    return (_tag(el), frozenset(_classes(el)), tuple(_tag(c) for c in _kids(el)))


def _chromey(el: Node) -> bool:
    node: "Node | None" = el
    hops = 0
    while node is not None and hops < 25:
        if _tag(node) in _CHROME_TAGS or (node.get("role") or "").lower() in _CHROME_ROLES:
            return True
        node = node.getparent()
        hops += 1
    return False


def _richness(members: "list[Node]") -> float:
    """Average descendant count of a sample, scaled to 0..1 (a bare-link menu is thin; cards rich)."""
    sample = members[:20]
    total = 0
    for m in sample:
        c = 0
        for _ in m.iter():
            c += 1
            if c >= 12:
                break
        total += c
    return min(total / len(sample), 10.0) / 10.0 if sample else 0.0


def _container_selector(container: Node) -> str:
    """The container as a durable selector: ``#id`` when it has one, else its tag plus up to two
    class hooks (``ul[class*="Stack"]``), else the bare tag."""
    node_id = container.get("id")
    if node_id:
        return f"#{node_id}"
    return _tag(container) + "".join(_classes(container)[:2])


def _item_selector(members: "list[Node]") -> str:
    """A selector for the records: the unwrapped tag, narrowed by a durable class hook common to
    ALL members (a semantic class, or the stem of a generated one -- never the hash itself)."""
    unwrapped = [_unwrap(m) for m in members]
    tag = _tag(unwrapped[0])
    common = set(_classes(unwrapped[0]))
    for m in unwrapped[1:]:
        common &= set(_classes(m))
    if common:
        order = _classes(unwrapped[0])
        return f"{tag}{sorted(common, key=lambda c: (order.index(c), -len(c)))[0]}"
    return tag


def scan(doc: "Document", *, min_items: int = 3) -> "list[tuple[Node, RecordRegion]]":
    """Every qualifying region as (container node, :class:`RecordRegion`), best first -- the shared
    search behind :func:`find_records` and the skeleton's record marks (which need the node).
    """
    if not doc._markup():
        return []
    found: list[tuple[Node, RecordRegion]] = []
    for container in doc._root().iter():
        if _tag(container) in _DRAWING | _CONTROLS or any(
            _tag(a) in _DRAWING | _CONTROLS for a in container.iterancestors()
        ):
            continue
        children = _kids(container)
        if len(children) < min_items:
            continue
        groups: dict[tuple[str, frozenset[str], tuple[str, ...]], list[Node]] = {}
        for c in children:
            groups.setdefault(_sig(c), []).append(c)
        penalty = 0.25 if _chromey(container) else 1.0
        for members in groups.values():
            if len(members) < min_items:
                continue
            score = len(members) * (1.0 + _richness(members)) * penalty
            found.append(
                (
                    container,
                    RecordRegion(
                        item_selector=_item_selector(members),
                        container_selector=_container_selector(container),
                        count=len(members),
                        container_tag=_tag(container),
                        container_id=container.get("id") or "",
                        score=round(score, 3),
                    ),
                )
            )
    found.sort(key=lambda pair: pair[1].score, reverse=True)
    return found


def find_records(doc: "Document", *, min_items: int = 3, top_k: int = 3) -> "list[RecordRegion]":
    """The most dataset-like repeating regions in a markup document, best first (up to ``top_k``).
    A region is a container plus a group of >= ``min_items`` structurally-identical children; scored
    so a long, content-rich, non-chrome list outranks a short nav menu."""
    return [region for _node, region in scan(doc, min_items=min_items)][:top_k]


__all__ = ["RecordRegion", "find_records", "scan"]
