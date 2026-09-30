"""Timeliness -- a DETERMINISTIC read of whether an extracted, dated dataset carries its most
RECENT rows, relative to how often rows appear.

A FLAG for the human review, never a ship blocker: ``stale`` is True when the gap from the newest
row to today is far larger than the typical interval BETWEEN rows -- i.e. the latest items are
missing (client-rendered, or behind a tab / filter / page the query did not reach). This is a
timeliness bar, NOT a completeness one: older rows missing is fine; only the LATEST must be there.
No date field, or no parseable dates -> nothing to judge. Stdlib only.
"""

from __future__ import annotations

import datetime as _dt
import re
import statistics
from collections.abc import Sequence

from pydantic import JsonValue

from .models import DatasetBrief

#: stale when the newest row is older than this many typical inter-row gaps (and > a week).
_INTERVALS = 3
_DATE_TYPES = ("date", "time")
_DATE_NAMES = ("date", "datetime", "time", "published", "updated", "posted", "when")
_MONTHS = {
    m: i
    for i, m in enumerate(
        ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1
    )
}
_ISO = re.compile(r"(\d{4})-(\d{2})-(\d{2})")
_DMY = re.compile(r"(\d{1,2})\s+([A-Za-z]{3})[a-z]*\.?,?\s+(\d{4})")
_MDY = re.compile(r"([A-Za-z]{3})[a-z]*\.?\s+(\d{1,2}),?\s+(\d{4})")


def date_fields(brief: DatasetBrief) -> "list[str]":
    """The brief's fields that carry a date: typed as a date/time, or named like one."""
    out: list[str] = []
    for f in brief.fields:
        typ = brief.types.get(f, "").lower()
        if any(t in typ for t in _DATE_TYPES) or f.lower() in _DATE_NAMES:
            out.append(f)
    return out


def parse_date(value: str) -> "_dt.date | None":
    """A calendar date out of a value: ISO (``2026-09-30``, or a full timestamp), ``30 Sep 2026``
    or ``Sep 30, 2026``. ``None`` when no date is recognisable."""
    m = _ISO.search(value)
    if m:
        try:
            return _dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    m = _DMY.search(value)
    if m and m.group(2).lower() in _MONTHS:
        try:
            return _dt.date(int(m.group(3)), _MONTHS[m.group(2).lower()], int(m.group(1)))
        except ValueError:
            return None
    m = _MDY.search(value)
    if m and m.group(1).lower() in _MONTHS:
        try:
            return _dt.date(int(m.group(3)), _MONTHS[m.group(1).lower()], int(m.group(2)))
        except ValueError:
            return None
    return None


def timeliness(rows: "Sequence[JsonValue]", brief: DatasetBrief) -> "tuple[str, bool]":
    """``(note, stale)`` for the extracted rows (see the module docstring). ``("", False)`` when the
    brief has no date field or no row carries a parseable date."""
    fields = date_fields(brief)
    if not fields:
        return "", False
    dates: set[_dt.date] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        for f in fields:
            v = row.get(f)
            if isinstance(v, str) and (d := parse_date(v)) is not None:
                dates.add(d)
    if not dates:
        return "", False
    ordered = sorted(dates, reverse=True)
    today, newest = _dt.date.today(), ordered[0]
    age = (today - newest).days
    gaps = [(ordered[i] - ordered[i + 1]).days for i in range(len(ordered) - 1)]
    if gaps:  # cadence known -> compare the gap-to-now against the typical inter-row gap
        typical = max(1, int(statistics.median(gaps)))
        stale = age > max(_INTERVALS * typical, 7)
        if stale:
            return (
                f"rows appear about every {typical} day(s), but the newest is {newest.isoformat()} "
                f"({age} days ago) -- a gap far larger than that cadence, so the MOST RECENT items "
                "are likely MISSING (behind a tab / filter / page, or client-rendered)",
                True,
            )
        return (
            f"rows appear about every {typical} day(s) and the newest is {age} day(s) old -- "
            "within cadence, the latest data is present",
            False,
        )
    stale = age > 120  # a single dated row: only flag a clearly-old lone item
    return (
        f"the only datable row is {newest.isoformat()} ({age} days ago)"
        + ("; likely missing more recent data" if stale else "; recent enough"),
        stale,
    )


__all__ = ["date_fields", "parse_date", "timeliness"]
