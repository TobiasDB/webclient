"""``Field`` and ``Collection``: the two typed value leaves the DSL terminals return.

A ``Field[T]`` is a scalar leaf (a value plus whether it is present/ok, with the read helpers
``number`` / ``date`` / ``split`` / ``map``); a ``Collection[T]`` is a materialised set of results
(elements or extracted rows) with the PURE row-shaping helpers (``project`` / ``merge`` / ``limit``
/ ``distinct`` / ``documents``). Ported from the monolith's query surface so both versions carry
the SAME learnings -- typed generics (no ``Any`` leaks, no bare ``object``). The per-element
sub-expression evaluation behind ``extract`` / ``filter`` lives in the executor (:mod:`web.dsl.run`),
so this module stays a cycle-free, pure value layer.
"""

from __future__ import annotations

import datetime as _dt
import json
import re
from typing import Generic, Iterator, TypeVar, cast
from urllib.parse import urljoin

from dateutil import parser as _du_parser
from pydantic import JsonValue

# covariant: Field/Collection only ever *produce* T (iterate/index/get), never consume it.
T = TypeVar("T", covariant=True)

_MONTHS = {
    m: i + 1 for i, m in enumerate("jan feb mar apr may jun jul aug sep oct nov dec".split())
}
_UNITS = {
    "second": 1,
    "sec": 1,
    "minute": 60,
    "min": 60,
    "hour": 3600,
    "hr": 3600,
    "day": 86400,
    "week": 604800,
    "month": 2629800,
    "year": 31557600,
}
#: number words :meth:`Field.number` reads when a value has no digits (star ratings coded as words).
_NUMBER_WORDS = {
    w: i
    for i, w in enumerate(
        "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen "
        "sixteen seventeen eighteen nineteen twenty".split()
    )
}


def parse_when(
    value: object,
    *,
    format: str | None = None,
    dayfirst: bool = False,
    now: "_dt.datetime | None" = None,
) -> "_dt.datetime | None":
    """A datetime from a read (standard library only, so it parses the same everywhere): a
    datetime / date; an explicit ``format``; ISO 8601; ``18 Sep 2026`` / ``Sep 18, 2026`` (with an
    optional time); ``2026/09/18``, ``09/18/2026`` or (``dayfirst``) ``18/09/2026``; ``today`` /
    ``yesterday`` / ``tomorrow`` / ``N units ago``. None when nothing reads as a date.
    """
    if value is None:
        return None
    if isinstance(value, _dt.datetime):
        return value
    if isinstance(value, _dt.date):
        return _dt.datetime(value.year, value.month, value.day)
    text = " ".join(str(value).split())
    if not text:
        return None
    if format:
        try:
            return _dt.datetime.strptime(text, format)
        except ValueError:
            return None
    iso = re.search(
        r"\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?)?",
        text,
    )
    if iso:
        try:
            return _dt.datetime.fromisoformat(iso.group(0).replace("Z", "+00:00"))
        except ValueError:
            pass
    clock = re.search(r"(\d{1,2}):(\d{2})(?::(\d{2}))?\s*([ap]\.?m\.?)?", text, re.I)

    def at(y: int, mo: int, d: int) -> "_dt.datetime | None":
        h = mi = sec = 0
        if clock:
            h, mi, sec = (
                int(clock.group(1)),
                int(clock.group(2)),
                int(clock.group(3) or 0),
            )
            ampm = (clock.group(4) or "").lower().replace(".", "")
            if ampm == "pm" and h < 12:
                h += 12
            if ampm == "am" and h == 12:
                h = 0
        try:
            return _dt.datetime(y, mo, d, h, mi, sec)
        except ValueError:
            return None

    mon = r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?"
    m = re.search(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+" + mon + r",?\s+(\d{4})", text, re.I)
    if m:
        return at(int(m.group(3)), _MONTHS[m.group(2).lower()[:3]], int(m.group(1)))
    m = re.search(mon + r"\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})", text, re.I)
    if m:
        return at(int(m.group(3)), _MONTHS[m.group(1).lower()[:3]], int(m.group(2)))
    m = re.search(r"\b(\d{4})[/.](\d{1,2})[/.](\d{1,2})\b", text)
    if m:
        return at(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    m = re.search(r"\b(\d{1,2})[/.-](\d{1,2})[/.-](\d{2,4})\b", text)
    if m:
        a, b, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        y = y + 2000 if y < 100 else y
        d, mo = (a, b) if (dayfirst or a > 12) else (b, a)
        return at(y, mo, d)
    base = now or _dt.datetime.now()
    low = text.lower()
    for word, days in (("today", 0), ("yesterday", -1), ("tomorrow", 1)):
        if re.search(rf"\b{word}\b", low):
            day = (base + _dt.timedelta(days=days)).date()
            return at(day.year, day.month, day.day)
    m = re.search(
        r"\b(\d+|an?|one)\s+(second|sec|minute|min|hour|hr|day|week|month|year)s?\s+ago\b",
        low,
    )
    if m:
        n = 1 if m.group(1) in ("a", "an", "one") else int(m.group(1))
        return (base - _dt.timedelta(seconds=n * _UNITS[m.group(2)])).replace(microsecond=0)
    return _fuzzy_when(text, base=base, dayfirst=dayfirst)


#: timezone ABBREVIATIONS a page writes next to a clock (``17:09 BST``) -> UTC offset in seconds,
#: for :mod:`dateutil` (which knows none by default). Ambiguous ones (IST, CST-China) are left out.
_TZ_OFFSETS: "dict[str, int]" = {
    "UTC": 0,
    "GMT": 0,
    "Z": 0,
    "BST": 3600,
    "WET": 0,
    "WEST": 3600,
    "CET": 3600,
    "CEST": 7200,
    "EET": 7200,
    "EEST": 10800,
    "EST": -18000,
    "EDT": -14400,
    "CST": -21600,
    "CDT": -18000,
    "MST": -25200,
    "MDT": -21600,
    "PST": -28800,
    "PDT": -25200,
    "AKST": -32400,
    "AKDT": -28800,
    "HST": -36000,
    "AEST": 36000,
    "AEDT": 39600,
    "JST": 32400,
    "SGT": 28800,
    "HKT": 28800,
}


def _fuzzy_when(text: str, *, base: "_dt.datetime", dayfirst: bool) -> "_dt.datetime | None":
    """The :mod:`dateutil` FUZZY read -- the fallback after the exact forms: a time on its own
    (``17:09 BST``) is TODAY at that time (``base``'s date, midnight-defaulted); a clock inside
    prose (``published at 17:48 BST``) is found among the words; a timezone abbreviation becomes an
    offset. Text without a digit never parses (prose that merely mentions "May")."""
    if not any(ch.isdigit() for ch in text):
        return None
    default = base.replace(hour=0, minute=0, second=0, microsecond=0)
    try:
        when = _du_parser.parse(
            text, fuzzy=True, dayfirst=dayfirst, default=default, tzinfos=_TZ_OFFSETS
        )
    except (ValueError, OverflowError):
        return None
    return when.replace(microsecond=0)


class Field(Generic[T]):
    """A scalar leaf: a value plus whether it is present/ok."""

    __slots__ = ("_value", "_ok", "_base")

    def __init__(self, value: object = None, *, ok: bool = True, base: str = "") -> None:
        self._value = value
        self._ok = ok and value is not None
        self._base = base  # the page URL the value was read on, so link() resolves relative text

    def get(self, default: object = None) -> T:
        """The field's value, or ``default`` when it is empty/missing."""
        return cast(T, self._value if self._ok else default)

    @property
    def value(self) -> T:
        """The raw stored value (without the empty/missing fallback ``get`` applies)."""
        return cast(T, self._value)

    @property
    def ok(self) -> bool:
        """Whether the field is present (matched and non-None)."""
        return self._ok

    def is_ok(self) -> "Field[bool]":
        """A boolean ``Field`` of whether this field is present -- for use inside a lazy filter."""
        return Field(self._ok)

    def is_empty(self) -> "Field[bool]":
        """A boolean ``Field`` of whether this field is empty by PRESENCE (missing / ``""`` / ``[]``
        / ``{}`` / ``None``) -- a matched ``0`` / ``False`` is NOT empty, unlike ``bool(field)``.
        """
        return Field(not self._ok or self._value in ("", [], {}, None))

    def number(self, default: object = None) -> "Field[float | int]":
        """The value as a NUMBER: the first number in the text (``"£51.77"`` -> 51.77, ``"22
        available"`` -> 22, ``"1,234"`` -> 1234), else a number WORD (``"Three"`` -> 3); ``default``
        when there is none."""
        v = self.get()
        if isinstance(v, bool):
            return Field(int(v))
        if isinstance(v, (int, float)):
            return Field(v)
        text = str(v or "")
        m = re.search(r"-?\d[\d,]*(?:\.\d+)?|-?\.\d+", text)
        if m:
            raw = m.group(0).replace(",", "")
            num = float(raw)
            return Field(int(num) if num.is_integer() and "." not in raw else num)
        for word in re.findall(r"[A-Za-z]+", text):
            if word.lower() in _NUMBER_WORDS:
                return Field(_NUMBER_WORDS[word.lower()])
        return Field(default)

    def date(
        self,
        format: str | None = None,
        *,
        dayfirst: bool = False,
        default: object = None,
    ) -> "Field[str]":
        """The value as a DATE, ``YYYY-MM-DD`` (ISO / written / numeric / relative text, or an
        explicit ``strptime`` ``format``); ``default`` when there is none."""
        when = parse_when(self.get(), format=format, dayfirst=dayfirst)
        return Field(when.date().isoformat() if when else default)

    def datetime(
        self,
        format: str | None = None,
        *,
        dayfirst: bool = False,
        default: object = None,
    ) -> "Field[str]":
        """The value as a DATETIME, ISO ``YYYY-MM-DDTHH:MM:SS`` -- the same inputs as :meth:`date`."""
        when = parse_when(self.get(), format=format, dayfirst=dayfirst)
        return Field(when.isoformat(timespec="seconds") if when else default)

    def link(self, base: str | None = None) -> "Field[str]":
        """The value as an absolute LINK URL: relative text resolved against ``base`` (else the page
        it was read on) -- for a URL written as text (a ``data-url``, a cell, ``"/x.html"``).
        """
        text = str(self.get() or "").strip()
        return Field(urljoin(base or self._base or "", text) if text else "")

    def regex(
        self, pattern: str, *, group: "int | str" = 0, flags: int = 0, default: object = None
    ) -> "Field[str]":
        """A substring of the value's TEXT: the first ``pattern`` match reduced to ``group`` --
        ``"Only $19.99!"`` -> ``.regex(r"\\$([\\d.]+)", group=1)`` -> ``"19.99"``; ``default``
        (``None``) when nothing matches. The leaf counterpart of a document's ``regex``."""
        text = str(self.get() if self.get() is not None else "")
        m = re.search(pattern, text, flags)
        return Field(m.group(group) if m else default, base=self._base)

    def map(self, mapping: "dict[str, JsonValue]", default: object = None) -> "Field[JsonValue]":
        """The value looked up in ``mapping`` (strings compare case-insensitively); ``default`` when
        it is not there."""
        v = self.get()
        key = v if isinstance(v, str) else str(v)
        if key in mapping:
            return Field(mapping[key])
        if isinstance(v, str):
            low = {str(k).lower(): val for k, val in mapping.items()}
            if v.strip().lower() in low:
                return Field(low[v.strip().lower()])
        return Field(default)

    def split(
        self,
        sep: str | None = None,
        maxsplit: int = -1,
        *,
        regex: bool = False,
        strip: bool = True,
        keep_empty: bool = False,
    ) -> "Collection[Field[str]]":
        """The text SPLIT into a Collection of Fields: on ``sep`` (whitespace when omitted; a regular
        expression with ``regex=True``), at most ``maxsplit`` times. Parts are stripped and empties
        dropped unless overridden. A missing value splits to an empty Collection."""
        if not self._ok:
            return Collection([])
        text = self._value if isinstance(self._value, str) else str(self._value)
        parts = (
            re.split(sep, text, maxsplit=max(maxsplit, 0))
            if (regex and sep is not None)
            else text.split(sep, maxsplit)
        )
        if strip:
            parts = [p.strip() for p in parts]
        if not keep_empty:
            parts = [p for p in parts if p != ""]
        return Collection([Field(p) for p in parts])

    def __bool__(self) -> bool:
        """Value TRUTHINESS (so ``0`` / ``False`` read as falsy) -- deliberately unlike
        :meth:`is_empty` (presence); lazy filters should prefer ``is_ok`` / ``is_empty``.
        """
        return bool(self._value) if self._ok else False

    def __eq__(self, o: object) -> bool:
        """Compare by underlying value (unwrapping the other operand if it is a ``Field`` too)."""
        return bool(self.get() == (o.get() if isinstance(o, Field) else o))

    def __ne__(self, o: object) -> bool:
        return not self.__eq__(o)

    def __hash__(self) -> int:
        return hash(self._value) if self._ok else 0

    def __repr__(self) -> str:
        return f"Field({self._value!r})" if self._ok else "Field(<empty>)"


class Ref:
    """A resolvable reference value: an (absolute) URL that ``.resolve()`` fetches into a Document.
    ``attr('href')`` / ``attr('src')`` and ``links()`` yield these, so a link can be FOLLOWED
    (``select('a').attr('href').resolve()...``); collected without resolving, a Ref reads as its
    URL string."""

    __slots__ = ("url", "base")

    def __init__(self, url: str = "", base: str = "") -> None:
        self.url = url
        self.base = base

    def __repr__(self) -> str:
        return f"Ref({self.url!r})"


def raw(value: object) -> JsonValue:
    """Unwrap a ``Field`` to its raw value (missing -> None); a ``Ref`` to its URL; pass anything
    else through, cleaned to plain JSON data (a list is cleaned item-wise) -- the shape a projected
    row stores."""
    if isinstance(value, Field):
        return raw(value.get())
    if isinstance(value, Ref):
        return value.url
    if isinstance(value, (list, Collection)):
        return [raw(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None or isinstance(value, dict):
        return cast(JsonValue, value)
    return str(value)  # a stray core value (e.g. an element) becomes its repr; rows stay pure data


def clean_row(row: "dict[str, object]") -> "dict[str, JsonValue]":
    """A copy of ``row`` with each value cleaned to plain JSON (see :func:`raw`)."""
    return {k: raw(v) for k, v in row.items()}


def distinct_rows(rows: "list[JsonValue]") -> "list[JsonValue]":
    """``rows`` with duplicates dropped, keeping the FIRST occurrence and preserving order (compared
    by a stable JSON key so nested data compares structurally) -- so an overlapping paginated walk,
    or a sticky record repeated on every page, yields each record once."""
    seen: set[str] = set()
    out: list[JsonValue] = []
    for r in rows:
        try:
            key = json.dumps(r, sort_keys=True, default=str)
        except (TypeError, ValueError):
            key = repr(r)
        if key not in seen:
            seen.add(key)
            out.append(r)
    return out


class Collection(Generic[T]):
    """A materialised set of results (elements or extracted rows). Iterable / indexable, with the
    PURE row-shaping helpers. ``extract`` / ``filter`` (which evaluate sub-expressions per element)
    are executor steps, not methods here -- so this stays a cycle-free value layer."""

    __slots__ = ("_items", "_rows", "base")

    def __init__(
        self,
        items: "list[T] | None" = None,
        *,
        rows: "list[dict[str, object]] | None" = None,
        base: str = "",
    ) -> None:
        self._items: list[T] = list(items or [])
        #: the extracted row per item (parallel to ``_items``), set by the executor's ``extract``
        #: step; ``None`` until an extract has run.
        self._rows = rows
        self.base = base

    def __iter__(self) -> Iterator[T]:
        return iter(self._items)

    def __len__(self) -> int:
        return len(self._items)

    def __getitem__(self, i: int) -> T:
        return self._items[i]

    def __repr__(self) -> str:
        return f"Collection({len(self._items)} items)"

    def derive(
        self, items: "list[object]", rows: "list[dict[str, object]] | None" = None
    ) -> "Collection[T]":
        """A new collection of ``items`` (and optional ``rows``) inheriting this one's base URL -- the
        shared way the executor and the shaping helpers return a transformed collection.
        """
        return Collection(cast("list[T]", items), rows=rows, base=self.base)

    def limit(self, n: int) -> "Collection[T]":
        """Keep at most the first ``n`` items (and their rows)."""
        return self.derive(
            cast("list[object]", self._items[:n]),
            self._rows[:n] if self._rows is not None else None,
        )

    def merge(self) -> "dict[str, JsonValue]":
        """Fold the items' extracted rows into ONE dict (later rows win on a repeated key) -- the
        shape of a key/value table."""
        out: dict[str, JsonValue] = {}
        for row in self._rows or []:
            out.update(clean_row(row))
        return out

    def documents(self, column: str) -> "Collection[object]":
        """Flatten a column whose values are Collections/lists into one Collection of those items."""
        out: list[object] = []
        for row in self._rows or []:
            value = row.get(column)
            out.extend(list(value) if isinstance(value, (list, Collection)) else [])
        return self.derive(out)

    def project(
        self, *, flatten: bool = False, sep: str = ".", distinct: bool = False
    ) -> "list[JsonValue]":
        """Materialise as a plain list: each item's extracted row cleaned to plain data (a ``Field``
        column becomes its value), or the item itself when it has no row. ``flatten`` merges nested
        dict columns into each row; ``distinct`` drops duplicate rows (first kept, order preserved).
        """
        out: list[JsonValue] = []
        rows = self._rows
        for i, item in enumerate(self._items):
            if rows is not None:
                row = clean_row(rows[i])
                out.append(_flatten(row, sep) if flatten else row)
            else:
                out.append(raw(item))
        return distinct_rows(out) if distinct else out


def _flatten(row: "dict[str, JsonValue]", sep: str) -> "dict[str, JsonValue]":
    """Merge every nested dict column into ``row``, its keys prefixed ``column{sep}key`` (one level;
    lists are left as they are)."""
    out: dict[str, JsonValue] = {}
    for k, v in row.items():
        if isinstance(v, dict):
            for ik, iv in v.items():
                out[f"{k}{sep}{ik}"] = iv
        else:
            out[k] = v
    return out


__all__ = [
    "Field",
    "Collection",
    "Ref",
    "parse_when",
    "raw",
    "clean_row",
    "distinct_rows",
]
