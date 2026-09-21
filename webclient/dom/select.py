"""CSS / XPath selection scoped to a node (the ``select`` primitive)."""

from __future__ import annotations

import re
from typing import Any

__all__ = ["find"]

_BARE_TAG = re.compile(r"^[A-Za-z_][\w-]*$")  # a lone element-name selector (no combinators)
_ASCII_UPPER = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
_ASCII_LOWER = "abcdefghijklmnopqrstuvwxyz"


def find(root: Any, selector: str, *, xml: bool = False) -> list[Any]:
    """Resolve a CSS or XPath ``selector`` to the matching elements, scoped to ``root``.
    Rejects attribute/text selectors (use ``attr``), scopes a leading ``//`` to the current
    node, and (``xml=True``) falls back to a case-insensitive local-name match for a bare tag."""
    if selector.rstrip().endswith(("text()",)) or "/@" in selector:
        raise ValueError(
            "select yields elements; use .attr() for an attribute or text"
        )
    if selector.startswith("//"):
        # lxml: element.xpath("//...") searches the WHOLE document, not the element.
        # A leading "//" inside a selected record means "descendant of THIS node", so
        # scope it with a leading "." (harmless at document level -- same result).
        selector = "." + selector
    if selector.startswith("/") or selector.startswith("./"):
        return list(root.xpath(selector))
    matches = list(root.cssselect(selector))
    if not matches and xml and _BARE_TAG.match(selector.strip()):
        # XML element names are case-SENSITIVE, but scrapers (and LLMs) lowercase them and
        # RSS/Atom feeds spell them pubDate/lastBuildDate/... . When an exact match found
        # nothing, fall back to a case-insensitive local-name match for a bare tag so a
        # lowercased field tag still resolves (local-name() also ignores any ns prefix).
        tag = selector.strip().lower()
        matches = list(root.xpath(
            f".//*[translate(local-name(),{_ASCII_UPPER!r},{_ASCII_LOWER!r})=$t]", t=tag,
        ))
    return matches
