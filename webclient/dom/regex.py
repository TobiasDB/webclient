"""Regex extraction over a string value (the ``attr(..., pattern=)`` primitive)."""

from __future__ import annotations

import re

__all__ = ["regex_extract"]


def regex_extract(value: str, pattern: str, group: "int | str | None") -> "str | None":
    """Search ``value`` for ``pattern`` and return the requested ``group`` (an index or
    named group; ``None`` -> group 1 when the pattern captures, else the whole match).
    ``None`` when the pattern does not match, or the group is absent."""
    m = re.search(pattern, value)
    if m is None:
        return None
    if group is not None:
        try:
            return m.group(group)
        except IndexError:  # a bad group index / unknown group name
            return None
    return m.group(1) if m.groups() else m.group(0)

