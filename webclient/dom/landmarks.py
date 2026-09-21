"""Page landmarks: which region (nav / main / article / header / footer / aside) an element
sits in, by sectioning tag, ARIA role, or class/id hint."""

from __future__ import annotations

from typing import Any

from .parse import tag as _tag

__all__ = ["LANDMARKS", "LANDMARK_ROLES", "LANDMARK_HINTS", "landmark_of"]

#: the page landmarks ``region`` classifies an element into (the HTML sectioning
#: elements + their ARIA-role and class/id equivalents).
LANDMARKS = frozenset({"nav", "main", "article", "header", "footer", "aside"})

#: ARIA landmark ``role`` -> the landmark it denotes.
LANDMARK_ROLES = {
    "navigation": "nav",
    "main": "main",
    "article": "article",
    "banner": "header",
    "contentinfo": "footer",
    "complementary": "aside",
}

#: class / id substring hints, tried (in order) when an ancestor has no landmark
#: tag or ARIA role -- the first hit classifies the region.
LANDMARK_HINTS = (
    ("footer", "footer"),
    ("masthead", "header"),
    ("breadcrumb", "nav"),
    ("menu", "nav"),
    ("nav", "nav"),
    ("sidebar", "aside"),
)



def landmark_of(el: Any, *, max_hops: int = 25) -> str:
    """The landmark ``el`` sits in -- ``nav`` / ``main`` / ``article`` / ``header`` / ``footer``
    / ``aside`` (or ``""`` if none) -- found by walking its ancestors for the nearest landmark
    tag, ARIA ``role``, or class/id hint."""
    node = el
    hops = 0
    while node is not None and hops < max_hops:
        tag = _tag(node).rsplit("}", 1)[-1]  # strip any XML namespace
        if tag in LANDMARKS:
            return tag
        role = (node.get("role") or "").strip().lower() if hasattr(node, "get") else ""
        if role in LANDMARK_ROLES:
            return LANDMARK_ROLES[role]
        hint = (
            f"{node.get('class') or ''} {node.get('id') or ''}".lower()
            if hasattr(node, "get")
            else ""
        )
        if hint.strip():
            for needle, landmark in LANDMARK_HINTS:
                if needle in hint:
                    return landmark
        node = node.getparent()
        hops += 1
    return ""
