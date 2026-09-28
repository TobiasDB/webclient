"""JSON navigation -- dotted-path selection and a shape outline, for JSON documents.

A JSON API response is data the same way a page is: you need to find where the records live and pull
fields out. :func:`dig` follows a dotted path (``data.results[0].name``, ``items[2]``); :func:`skeleton`
outlines the shape (keys + value types, an array as ``[N]`` with its element shape) so a query can be
written against it, the JSON twin of the DOM skeleton.
"""

from __future__ import annotations

import re
from typing import Any

_INDEX = re.compile(r"^(.*?)\[(\d+)\]$")  # a path segment with a trailing [n]


def dig(value: Any, path: str) -> Any:
    """Follow a dotted path into a JSON value: ``"a.b"`` keys, ``"items[0]"`` indexes, ``""`` the
    value itself. A missing key / wrong type / out-of-range index yields ``None``."""
    if not path:
        return value
    for seg in path.split("."):
        m = _INDEX.match(seg)
        key, idx = (m.group(1), int(m.group(2))) if m else (seg, None)
        if key:
            value = value.get(key) if isinstance(value, dict) else None
        if idx is not None:
            value = value[idx] if isinstance(value, list) and -len(value) <= idx < len(value) else None
        if value is None:
            return None
    return value


def leaves(value: Any, *, budget: int = 20000) -> "list[str]":
    """Every scalar leaf (string/number/bool) of a JSON value, as strings -- bounded by ``budget``."""
    out: list[str] = []
    _walk(value, out, budget)
    return out


def _walk(value: Any, out: "list[str]", budget: int) -> None:
    if len(out) >= budget:
        return
    if isinstance(value, dict):
        for v in value.values():
            _walk(v, out, budget)
    elif isinstance(value, list):
        for v in value:
            _walk(v, out, budget)
    elif isinstance(value, bool):
        out.append("true" if value else "false")
    elif isinstance(value, (str, int, float)):
        out.append(str(value))


def _type(v: Any) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, (int, float)):
        return "number"
    return "string"


def _merge_keys(items: "list[Any]") -> "dict[str, Any]":
    """A representative object for an array of objects: the union of keys with a sample value each."""
    merged: dict[str, Any] = {}
    for it in items:
        if isinstance(it, dict):
            for k, v in it.items():
                if k not in merged or merged[k] in (None, "", [], {}):
                    merged[k] = v
    return merged


def skeleton(value: Any, *, max_lines: int = 400, text_chars: int = 40, max_depth: int = 30) -> str:
    """A token-lean JSON shape outline: keys with value types, an array as ``[N]`` with its element
    shape (object keys merged across items) -- so a query can be written by dotted path."""
    lines: list[str] = []

    def sample(v: Any) -> str:
        s = str(v)
        return s if len(s) <= text_chars else s[:text_chars] + "…"

    def walk(v: Any, key: str, depth: int) -> None:
        if len(lines) >= max_lines or depth > max_depth:
            return
        pad = "  " * depth
        label = f"{key}: " if key else ""
        if isinstance(v, dict):
            lines.append(f"{pad}{label}{{}}" if not v else f"{pad}{label}{{")
            for k, item in v.items():
                walk(item, k, depth + 1)
            if v:
                lines.append(f"{pad}}}")
        elif isinstance(v, list):
            lines.append(f"{pad}{label}[{len(v)}]")
            if v:
                rep = _merge_keys(v) if any(isinstance(i, dict) for i in v) else v[0]
                walk(rep, "", depth + 1)
        else:
            lines.append(f"{pad}{label}{_type(v)}  = {sample(v)}")

    walk(value, "", 0)
    return "\n".join(lines[:max_lines])


__all__ = ["dig", "leaves", "skeleton"]
