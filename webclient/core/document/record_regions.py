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

from .html import _is_noise_class  # the shared hashed-class definition (read-only import)

_SKIP = frozenset({"script", "style", "noscript", "template", "svg", "path", "br", "hr"})
_CHROME_TAGS = frozenset({"nav", "header", "footer", "aside"})
_CHROME_ROLES = frozenset({"navigation", "banner", "contentinfo", "complementary"})


class RecordRegion(BaseModel):
    """A detected repeating region -- a dataset candidate."""

    item_selector: str  # a suggested ``select_all`` target, e.g. "li.item" / "div.card"
    count: int  # how many records
    container_tag: str = ""
    container_id: str = ""
    score: float = 0.0  # count x content-richness x (chrome penalty) -- higher = more dataset-like


def _tag(el: Any) -> str:
    return el.tag.lower() if isinstance(getattr(el, "tag", None), str) else ""


def _semantic_classes(el: Any) -> "list[str]":
    return [c for c in str(el.get("class") or "").split() if not _is_noise_class(c)]


def _child_tags(el: Any) -> "tuple[str, ...]":
    return tuple(_tag(c) for c in el if isinstance(c.tag, str) and _tag(c) not in _SKIP)


def _sig(el: Any) -> "tuple[str, frozenset[str], tuple[str, ...]]":
    """A light structural signature: tag + semantic classes + immediate child tag shape.
    Two siblings share a signature when they are the same KIND of record."""
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
    links is thin; a card list is rich)."""
    if not members:
        return 0.0
    avg = sum(sum(1 for _ in m.iter()) for m in members) / len(members)
    return min(avg, 10.0) / 10.0


def _item_selector(members: "list[Any]") -> str:
    """A selector for the records: the shared tag, narrowed by a class common to ALL of
    them (the most specific stable hook), else the bare tag."""
    tag = _tag(members[0])
    common = set(_semantic_classes(members[0]))
    for m in members[1:]:
        common &= set(_semantic_classes(m))
    if common:
        # prefer the longest shared class (usually the most specific / least generic)
        return f"{tag}.{sorted(common, key=len)[-1]}"
    return tag


def find_record_regions(
    root: Any, *, min_items: int = 3, top_k: int = 3
) -> "list[RecordRegion]":
    """The most dataset-like repeating regions under ``root``, best first. A region is a
    container plus a group of >= ``min_items`` structurally-identical children; scored by
    ``count x (1 + richness) x chrome_penalty`` so a long, content-rich, non-chrome list
    outranks a short nav menu. Returns up to ``top_k``."""
    regions: list[RecordRegion] = []
    for container in root.iter():
        if not isinstance(getattr(container, "tag", None), str):
            continue
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
            regions.append(
                RecordRegion(
                    item_selector=_item_selector(members),
                    count=len(members),
                    container_tag=_tag(container),
                    container_id=container.get("id") or "",
                    score=round(score, 3),
                )
            )
    regions.sort(key=lambda r: r.score, reverse=True)
    return regions[:top_k]


__all__ = ["RecordRegion", "find_record_regions"]
