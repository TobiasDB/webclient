"""RegexBacking: pull patterns out of a document's text.

``regex`` returns the first match (a capture group, or the whole match) as a
``Field``; ``regex_all`` returns every match. Both read the document's
``text_content`` (a selected sub-element narrows it first), so extracting a
value/unit out of a messy string is one op in a query::

    doc.select(".price").regex(r"[\\d.]+")            # "30"
    doc.select(".price").regex(r"[\\d.]+\\s*(\\w+)", group=1)   # "TB"

Isolated in its own backing so pattern extraction is additive and never entangles
with the medium backings.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from ...collection import Field
from ..web_core import Backing

if TYPE_CHECKING:
    from . import Document

_FLAGS = {"i": re.IGNORECASE, "m": re.MULTILINE, "s": re.DOTALL, "x": re.VERBOSE}


def _compile(pattern: str, flags: str) -> "re.Pattern[str]":
    bits = 0
    for ch in flags:
        bits |= _FLAGS.get(ch.lower(), 0)
    return re.compile(pattern, bits)


def _group(match: "re.Match[str]", group: int | str) -> str | None:
    try:
        return match.group(group)
    except IndexError:  # a bad group name / index -> no value
        return None


class RegexBacking(Backing):
    """Regex extraction over a document's text: ``regex`` (first) / ``regex_all``."""

    provides = frozenset({"regex", "regex_all"})
    gate = "ok"

    def applies(self, core: "Document") -> bool:
        return True  # text_content is available on any resolved document

    def regex(
        self, core: "Document", pattern: str, *, group: int | str = 0, flags: str = ""
    ) -> "Field[str]":
        """The first match of ``pattern`` in the document's text, as a ``Field``:
        ``group`` picks a capture group (``0`` = the whole match, an int index, or a
        named group); ``flags`` is any of ``"imsx"``. Empty ``Field`` when nothing
        matches (so ``.is_empty()`` / a lenient extract works)."""
        text = core.text_content or ""
        m = _compile(pattern, flags).search(text)
        value = _group(m, group) if m is not None else None
        return Field(value, ok=value is not None)

    def regex_all(
        self, core: "Document", pattern: str, *, group: int | str = 0, flags: str = ""
    ) -> "list[str]":
        """Every match of ``pattern`` in the document's text (the chosen ``group`` of
        each), in order -- e.g. all prices on a page. Empty list when none match."""
        text = core.text_content or ""
        out: list[str] = []
        for m in _compile(pattern, flags).finditer(text):
            g = _group(m, group)
            if g is not None:
                out.append(g)
        return out


__all__ = ["RegexBacking"]
