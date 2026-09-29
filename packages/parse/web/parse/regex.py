"""Regex extraction over a :class:`~web.parse.Document` -- the escape hatch for facts that live in
free text, not in a clean DOM node (a price inside a sentence, an id in a script blob, an inline
JSON value). Runs against the document's decoded text.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .document import Document


def regex(
    doc: "Document", pattern: str, *, group: "int | str" = 0, flags: int = 0
) -> "str | None":
    """The FIRST match of ``pattern`` in the document's text, returning ``group`` (0 = whole match,
    an int/name = a capture group), or ``None`` if it does not match."""
    m = re.search(pattern, doc.text, flags)
    if m is None:
        return None
    try:
        return m.group(group)
    except IndexError:  # a group index/name that isn't in the pattern
        return None


def regex_all(
    doc: "Document", pattern: str, *, group: "int | str" = 0, flags: int = 0
) -> "list[str]":
    """EVERY non-overlapping match of ``pattern``, each reduced to ``group``."""
    out: list[str] = []
    for m in re.finditer(pattern, doc.text, flags):
        try:
            val = m.group(group)
        except IndexError:
            continue
        if val is not None:
            out.append(val)
    return out


__all__ = ["regex", "regex_all"]
