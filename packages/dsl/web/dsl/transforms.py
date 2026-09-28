"""Post-extraction transforms -- the DSL reads that shape EXTRACTED DATA rather than the DOM.

After ``select_all(...).project(...)`` (or ``text``/``links``) the plan holds a list of row dicts or
scalars, not elements. These ops clean and reshape that data: collection ops (``filter`` /
``distinct`` / ``limit`` / ``merge`` / ``nonempty``) and per-value ops (``number`` / ``date`` /
``split`` / ``regex`` / ``strip``). A per-value op takes an optional ``field`` to target one column
of row dicts; without it, it maps over a scalar list (or the single value). All args are plain
JSON, so a plan carrying them still serialises to a blob for remote dispatch.
"""

from __future__ import annotations

import re
from typing import Any

from dateutil import parser as _dateparser

#: the op names handled here (vs the element reads applied in engine._apply_read).
TRANSFORMS = frozenset({"filter", "distinct", "limit", "merge", "nonempty",
                        "number", "date", "split", "regex", "strip"})

_NUM = re.compile(r"-?\d[\d,]*\.?\d*")


def apply(value: Any, op: str, args: list[Any]) -> Any:
    """Dispatch one transform op over ``value``."""
    if op == "filter":
        return _filter(value, args[0])
    if op == "distinct":
        return _distinct(value, args[0] if args else None)
    if op == "limit":
        return value[: args[0]] if isinstance(value, list) else value
    if op == "merge":
        return _merge(value)
    if op == "nonempty":
        return _nonempty(value)
    # per-value ops: args = [{"field": ..., ...}]
    opts = args[0] if args else {}
    field = opts.get("field")
    fn = {
        "number": _to_number,
        "date": _to_date,
        "strip": lambda v: v.strip() if isinstance(v, str) else v,
        "split": lambda v: v.split(opts.get("sep")) if isinstance(v, str) else v,
        "regex": lambda v: _regex(v, opts.get("pattern", ""), opts.get("group", 0)),
    }[op]
    return _map_field(value, fn, field)


def _filter(value: Any, equals: dict[str, Any]) -> Any:
    """Keep row dicts whose named fields all equal the given values (case-insensitive substring for
    strings). Non-list / non-dict passes through."""
    if not isinstance(value, list):
        return value
    def keep(row: Any) -> bool:
        if not isinstance(row, dict):
            return True
        return all(_matches(row.get(k), v) for k, v in equals.items())
    return [r for r in value if keep(r)]


def _matches(actual: Any, wanted: Any) -> bool:
    if isinstance(actual, str) and isinstance(wanted, str):
        return wanted.lower() in actual.lower()
    return bool(actual == wanted)


def _distinct(value: Any, key: "str | None") -> Any:
    """Drop duplicate rows/scalars, preserving order; ``key`` dedupes row dicts by one field."""
    if not isinstance(value, list):
        return value
    seen: set[Any] = set()
    out: list[Any] = []
    for item in value:
        marker = item.get(key) if key and isinstance(item, dict) else _hashable(item)
        if marker in seen:
            continue
        seen.add(marker)
        out.append(item)
    return out


def _hashable(item: Any) -> Any:
    if isinstance(item, dict):
        return tuple(sorted((k, str(v)) for k, v in item.items()))
    if isinstance(item, list):
        return tuple(str(x) for x in item)
    return item


def _merge(value: Any) -> Any:
    """Flatten one level: a list of lists becomes one flat list."""
    if not isinstance(value, list):
        return value
    out: list[Any] = []
    for item in value:
        out.extend(item) if isinstance(item, list) else out.append(item)
    return out


def _nonempty(value: Any) -> Any:
    """Drop empty rows -- a dict with no truthy field, an empty/None scalar."""
    if not isinstance(value, list):
        return value
    def full(item: Any) -> bool:
        if isinstance(item, dict):
            return any(v not in (None, "", [], {}) for v in item.values())
        return item not in (None, "", [], {})
    return [i for i in value if full(i)]


def _map_field(value: Any, fn: Any, field: "str | None") -> Any:
    """Apply ``fn`` to one field of each row dict, or element-wise over a scalar list, or once."""
    if isinstance(value, list):
        if field is not None:
            return [{**r, field: fn(r.get(field))} if isinstance(r, dict) else r for r in value]
        return [fn(v) for v in value]
    return fn(value)


def _to_number(v: Any) -> "float | int | None":
    """The first number in a value (``"$1,299.00"`` -> ``1299.0``, ``"12 items"`` -> ``12``)."""
    if isinstance(v, (int, float)):
        return v
    if not isinstance(v, str):
        return None
    m = _NUM.search(v)
    if m is None:
        return None
    raw = m.group(0).replace(",", "")
    num = float(raw)
    return int(num) if num.is_integer() and "." not in raw else num


def _to_date(v: Any) -> "str | None":
    """Parse a date/time out of a value, normalised to ISO 8601, or ``None``."""
    if not isinstance(v, str) or not v.strip():
        return None
    try:
        return _dateparser.parse(v, fuzzy=True).isoformat()
    except (ValueError, OverflowError):
        return None


def _regex(v: Any, pattern: str, group: "int | str") -> "str | None":
    if not isinstance(v, str) or not pattern:
        return None
    m = re.search(pattern, v)
    if m is None:
        return None
    try:
        return m.group(group)
    except IndexError:
        return None


__all__ = ["TRANSFORMS", "apply"]
