"""Record-region detection: find the dominant repeating structure -- the DATASET.

The highest-leverage skeleton-deepening piece (see the roadmap): most "0 rows" failures
are the author picking the wrong container to ``select_all``. This module locates the
container whose children form the largest run of structurally-identical siblings and
proposes a concrete item selector, so the skeleton can say
``← RECORD LIST · 24 items · select_all("li.item")``.

Pure and static -- it runs on the parsed lxml tree, no browser. Wiring it into the
skeleton is a separate integration; this is the algorithm + its result model.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from .classes import is_noise_class as _is_noise_class
from .parse import tag as _tag

_SKIP = frozenset({"script", "style", "noscript", "template", "svg", "path", "br", "hr"})
_DRAWING = frozenset({"svg", "math", "canvas"})  # subtrees whose repetition is geometry, never records
_CHROME_TAGS = frozenset({"nav", "header", "footer", "aside"})
_CHROME_ROLES = frozenset({"navigation", "banner", "contentinfo", "complementary"})


class RecordRegion(BaseModel):
    """A detected repeating region -- a dataset candidate."""

    item_selector: str  # a suggested ``select_all`` target, e.g. "li.item" / "div.card"
    count: int  # how many records
    container_tag: str = ""
    container_id: str = ""
    score: float = 0.0  # count x content-richness x (chrome penalty) -- higher = more dataset-like


def _semantic_classes(el: Any) -> "list[str]":
    """The element's class tokens with framework/utility noise classes dropped -- the ones
    that might carry meaning for grouping records."""
    return [c for c in str(el.get("class") or "").split() if not _is_noise_class(c)]


def _child_tags(el: Any) -> "tuple[str, ...]":
    """The tag names of the element's meaningful child elements (skipping chrome tags) --
    a cheap shape signature used to spot sibling records of the same shape."""
    return tuple(_tag(c) for c in el if isinstance(c.tag, str) and _tag(c) not in _SKIP)


#: tags that can be an anonymous per-record WRAPPER (the SPA/Tailwind norm: each record sits alone
#: inside a class-less/utility-only div).
_WRAPPER_TAGS = frozenset({"div", "span", "li", "section", "article"})


def _unwrap(el: Any) -> Any:
    """Descend through single-element-child, class-less-or-utility-only wrappers to the semantic
    record inside -- so a record wrapped in an anonymous ``<div>`` (the SPA/Tailwind per-record
    wrapper) is detected as its inner ``article.product``, not a bare ``div``. Stops at a wrapper
    that has its OWN semantic class (it IS the record) or that has more than one element child.
    Bounded to a few levels."""
    for _ in range(4):
        if _tag(el) not in _WRAPPER_TAGS or _semantic_classes(el):
            break  # not an anonymous wrapper -> this element is the record
        kids = [c for c in el if isinstance(getattr(c, "tag", None), str) and _tag(c) not in _SKIP]
        if len(kids) != 1:
            break
        el = kids[0]
    return el


def _sig(el: Any) -> "tuple[str, frozenset[str], tuple[str, ...]]":
    """A light structural signature: tag + semantic classes + immediate child tag shape, taken on
    the UNWRAPPED record (so records wrapped in anonymous divs still group by their inner shape).
    Two siblings share a signature when they are the same KIND of record."""
    el = _unwrap(el)
    return (_tag(el), frozenset(_semantic_classes(el)), _child_tags(el))


def _is_chromey(el: Any) -> bool:
    """Whether ``el`` is inside a chrome landmark (nav/header/footer/aside) -- records there
    are menus, not the dataset."""
    node = el
    hops = 0
    while node is not None and hops < 25:
        if isinstance(getattr(node, "tag", None), str):
            if _tag(node) in _CHROME_TAGS:
                return True
            role = (node.get("role") or "").strip().lower() if hasattr(node, "get") else ""
            if role in _CHROME_ROLES:
                return True
        node = node.getparent() if hasattr(node, "getparent") else None
        hops += 1
    return False


def _richness(members: "list[Any]") -> float:
    """Average descendant-element count of the members, scaled to 0..1 (a menu of bare
    links is thin; a card list is rich). BOUNDED -- ``skeleton()`` is a hot path, so this
    samples the first few members and counts each subtree only up to the point the 0..1
    scale saturates (``min(avg, 10)/10``), never walking a whole large subtree."""
    if not members:
        return 0.0
    sample = members[:20]  # a representative average; don't scan thousands of rows
    total = 0
    for m in sample:
        c = 0
        for _ in m.iter():  # cap: past ~12 descendants the scaled score is already maxed
            c += 1
            if c >= 12:
                break
        total += c
    return min(total / len(sample), 10.0) / 10.0


def _item_selector(members: "list[Any]") -> str:
    """A selector for the records: the shared tag (of the UNWRAPPED record, so an anonymous
    wrapper yields the inner ``article.product`` not a bare ``div``), narrowed by a class common
    to ALL of them (the most specific stable hook), else the bare tag."""
    members = [_unwrap(m) for m in members]
    tag = _tag(members[0])
    common = set(_semantic_classes(members[0]))
    for m in members[1:]:
        common &= set(_semantic_classes(m))
    if common:
        # prefer the class the author listed FIRST (the semantic one: ``card`` in
        # ``class="card rounded border"``), then the longest (the most specific)
        order = _semantic_classes(members[0])
        return f"{tag}.{sorted(common, key=lambda c: (order.index(c), -len(c)))[0]}"
    return tag


def scan_regions(root: Any, min_items: int) -> "list[tuple[Any, RecordRegion]]":
    """Every qualifying region as (container element, RecordRegion), best first -- the
    shared search behind :func:`find_record_regions` and :func:`region_marks`."""
    found: list[tuple[Any, RecordRegion]] = []
    for container in root.iter():
        if not isinstance(getattr(container, "tag", None), str):
            continue
        if _tag(container) in _DRAWING or any(_tag(a) in _DRAWING for a in container.iterancestors()):
            continue  # a drawing's repeated groups / rects are not data records
        children = [c for c in container if isinstance(c.tag, str) and _tag(c) not in _SKIP]
        if len(children) < min_items:
            continue
        groups: dict[Any, list[Any]] = {}
        for c in children:
            groups.setdefault(_sig(c), []).append(c)
        chrome_penalty = 0.25 if _is_chromey(container) else 1.0
        for members in groups.values():
            if len(members) < min_items:
                continue
            score = len(members) * (1.0 + _richness(members)) * chrome_penalty
            found.append((
                container,
                RecordRegion(
                    item_selector=_item_selector(members),
                    count=len(members),
                    container_tag=_tag(container),
                    container_id=container.get("id") or "",
                    score=round(score, 3),
                ),
            ))
    found.sort(key=lambda pair: pair[1].score, reverse=True)
    return found


def find_record_regions(
    root: Any, *, min_items: int = 3, top_k: int = 3
) -> "list[RecordRegion]":
    """The most dataset-like repeating regions under ``root``, best first. A region is a
    container plus a group of >= ``min_items`` structurally-identical children; scored by
    ``count x (1 + richness) x chrome_penalty`` so a long, content-rich, non-chrome list
    outranks a short nav menu. Returns up to ``top_k``."""
    return [region for _el, region in scan_regions(root, min_items)][:top_k]


def xpath_of(el: Any) -> str:
    """A canonical XPath for an element -- a STABLE key across traversals (lxml element
    proxies do NOT have a stable ``id()``, so identity/``id()`` can't be used to match nodes
    between the scan and the skeleton walk)."""
    try:
        return str(el.getroottree().getpath(el))
    except Exception:  # noqa: BLE001 - a detached element: no path, no mark
        return ""


def region_marks(root: Any, *, min_items: int = 3, top_k: int = 2) -> "dict[str, str]":
    """A map ``canonical-xpath -> skeleton marker`` for the top NON-CHROME record regions, so
    the skeleton can flag the dataset in place
    (``← RECORD LIST · N items · select_all("li.item")``). Chrome regions (nav/menu) are
    skipped -- they are not the dataset."""
    marks: dict[str, str] = {}
    for el, region in scan_regions(root, min_items):
        if len(marks) >= top_k:
            break
        if _is_chromey(el):  # a nav/menu list is not a dataset
            continue
        path = xpath_of(el)
        if path:
            marks[path] = (
                f'  ← RECORD LIST · {region.count} items · select_all("{region.item_selector}")'
            )
    return marks


def mark_for(marks: "dict[str, str]", el: Any) -> str:
    """The marker for an element (``""`` if none) -- matches by canonical XPath, the stable key."""
    return marks.get(xpath_of(el), "") if marks else ""


__all__ = ["RecordRegion", "find_record_regions", "region_marks", "mark_for", "scan_regions", "xpath_of"]
