"""Pure JSON helpers: parse leniently, walk leaves, and outline a value's SHAPE.

No cores, no state -- functions over a parsed value. ``json_skeleton`` is the JSON twin of
:func:`webclient.dom.skeleton.skeleton`: a token-lean outline an LLM writes dotted-path
queries from.
"""

from __future__ import annotations

import json as _json
from typing import Any

__all__ = ["parse_json", "json_leaves", "json_type", "merge_keys", "json_skeleton"]


def parse_json(data: "str | bytes | None") -> Any:
    """Parse a JSON body, returning the value or ``None`` when it is empty or does not parse
    (a mislabelled / truncated body must never crash a lenient caller)."""
    if not data:
        return None
    try:
        return _json.loads(data)
    except (ValueError, RecursionError):
        return None


def json_leaves(data: Any, *, budget: int = 20000) -> "list[str]":
    """Every scalar leaf (string / number / bool) of a parsed JSON value, as strings -- the
    values a rendered node's text is most likely to echo. Bounded by ``budget`` so a huge
    blob can't blow up the caller."""
    out: list[str] = []
    _walk_leaves(data, out, budget)
    return out


def _walk_leaves(data: Any, out: "list[str]", budget: int) -> None:
    """Recurse ``data``, appending its scalar leaves to ``out`` until ``budget`` is reached."""
    if len(out) >= budget:
        return
    if isinstance(data, dict):
        for v in data.values():
            _walk_leaves(v, out, budget)
    elif isinstance(data, list):
        for v in data:
            _walk_leaves(v, out, budget)
    elif isinstance(data, bool):
        out.append("true" if data else "false")
    elif isinstance(data, (str, int, float)):
        out.append(str(data))


def json_type(v: Any) -> str:
    """The shape name of a JSON scalar (objects/arrays are rendered structurally)."""
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, (int, float)):
        return "number"
    return "string"


def merge_keys(items: list[Any]) -> "dict[str, Any]":
    """A representative object for an array of objects: the union of keys, each mapped
    to a sample value (so ``results[].name`` paths are visible from one element)."""
    merged: dict[str, Any] = {}
    for it in items:
        if isinstance(it, dict):
            for k, v in it.items():
                if k not in merged or merged[k] in (None, "", [], {}):
                    merged[k] = v
    return merged


def json_skeleton(
    data: Any, *, max_lines: int = 400, text_chars: int = 40, max_depth: int = 30
) -> str:
    """A token-lean JSON shape outline: keys with value types, an array as ``[N]`` with
    its element shape (object keys merged across items), nested paths kept -- so an LLM
    can write dotted-path queries (``select('results[0].name')`` / ``extract``). The
    JSON twin of the DOM skeleton; a sample scalar is shown, truncated to
    ``text_chars``."""
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
            if v:
                for k, item in v.items():
                    walk(item, k, depth + 1)
                lines.append(f"{pad}}}")
        elif isinstance(v, list):
            lines.append(f"{pad}{label}[{len(v)}]")
            if v:  # show one representative element's shape (merged object keys)
                rep = merge_keys(v) if any(isinstance(i, dict) for i in v) else v[0]
                walk(rep, "", depth + 1)
        else:
            lines.append(f"{pad}{label}{json_type(v)}  = {sample(v)}")

    walk(data, "", 0)
    return "\n".join(lines[:max_lines])

