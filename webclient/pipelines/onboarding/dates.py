"""onboarding.dates -- date parsing + the timeliness assessment (pure helpers shared by
the query and review stages)."""

from __future__ import annotations

import json
import logging
from typing import Any

from .artifacts import Brief
from .common import log  # noqa: F401


#: newest item older than this many TYPICAL inter-row intervals -> the latest data is
#: missing (a self-calibrating timeliness bar: a daily feed silent for weeks is stale,
#: a quarterly feed a couple months out is not).
_TIMELINESS_INTERVALS = 3

#: the completeness gate is KEPT but DISABLED by default -- flip this True to make the query
#: review fail a query that captured only a fraction of the records (e.g. one of many pages).
#: We are focused on TIMELINESS (the latest data) for now, not full-history completeness.
_CHECK_COMPLETENESS = False
_COMPLETENESS_BLOCK = (
    "- COMPLETENESS: the query should capture ALL the records the dataset covers -- if it "
    "returned only a fraction (one of several pages/tabs, a too-narrow record selector, "
    "pagination not followed), that is INCOMPLETE and must FAIL."
)
_COMPLETENESS_OFF = (
    "- Completeness is NOT required for this run: do NOT fail because older records or other "
    "pages/tabs are missing. Only the fields' CORRECTNESS and the TIMELINESS of the newest "
    "rows matter here."
)


def _parse_date(s: str) -> "Any":
    """A ``date`` from a human/ISO date string, or ``None``. Handles the common shapes
    (``December 18, 2025`` / ``Dec 18, 2025`` / ``2025-12-18`` / ``12/18/2025`` / ...)."""
    import datetime
    import re

    s = s.strip()
    for fmt in ("%B %d, %Y", "%b %d, %Y", "%B %d %Y", "%b %d %Y", "%b. %d, %Y",
                "%Y-%m-%d", "%m/%d/%Y", "%d %B %Y", "%d %b %Y", "%Y/%m/%d"):
        try:
            return datetime.datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    # RFC 822 (RSS <pubDate>: "Tue, 09 Sep 2026 13:00:00 GMT") -- a primary onboarding target.
    try:
        from email.utils import parsedate_to_datetime

        dt = parsedate_to_datetime(s)
        if dt is not None:
            return dt.date()
    except (TypeError, ValueError):
        pass
    # ISO 8601 with a time / offset (Atom <updated>: "2026-09-14T10:30:00Z").
    try:
        return datetime.datetime.fromisoformat(s.replace("Z", "+00:00")).date()
    except ValueError:
        pass
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        try:
            return datetime.date(int(m[1]), int(m[2]), int(m[3]))
        except ValueError:
            pass
    return None


#: leaf field names (or suffixes) that denote a date/time -- matched on the LAST dotted
#: segment (so "date"/"price.date" match, but "runtime"/"timezone" do not).
_DATE_LEAVES = ("date", "published", "pubdate", "datetime", "timestamp", "time", "year",
                "updated", "created")


def _date_field_paths(brief: Brief) -> "list[str]":
    """The brief's field PATHS (dotted) whose leaf is a date-like field -- including nested
    ones (``event.date``), matched on the leaf segment, not a loose substring anywhere."""
    out: list[str] = []
    for f in brief.fields:
        leaf = f.split(".")[-1].lower()
        if leaf in _DATE_LEAVES or leaf.endswith(("date", "_at")):
            out.append(f)
    return out


def _dig(row: Any, path: str) -> Any:
    """Follow a dotted ``path`` into a (possibly nested) row dict; ``None`` if any hop is
    missing or not a dict."""
    cur = row
    for seg in path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(seg)
    return cur


def _timeliness(rows: "list[Any]", brief: Brief) -> "tuple[str, bool]":
    """TIMELINESS for a dated dataset: is the newest row recent RELATIVE TO how often rows
    appear? Returns ``(note, stale)``. ``stale`` is True when the gap from the newest item
    to today is far larger than the typical interval BETWEEN rows -- i.e. the most-recent
    items are missing (the current data is likely client-rendered / behind a tab we didn't
    capture). No date field, or no parseable dates, -> ``("", False)`` (nothing to judge).
    This is a timeliness bar, NOT a completeness one: older rows / other pages missing is
    fine; only the LATEST data must be present."""
    import datetime
    import statistics

    date_paths = _date_field_paths(brief)  # dotted paths whose LEAF is a date-like field
    if not date_paths:
        return "", False
    dates = sorted(
        {d for r in rows if isinstance(r, dict) for p in date_paths
         if isinstance((v := _dig(r, p)), str) and (d := _parse_date(v)) is not None},
        reverse=True,
    )
    if not dates:
        return "", False
    today, newest = datetime.date.today(), dates[0]
    age = (today - newest).days
    gaps = [(dates[i] - dates[i + 1]).days for i in range(len(dates) - 1)]
    gaps = [g for g in gaps if g >= 0]
    if gaps:  # cadence known -> compare the gap-to-now against the typical inter-row gap
        typical = max(1, int(statistics.median(gaps)))
        stale = age > max(_TIMELINESS_INTERVALS * typical, 7)
        if stale:
            return (f"TIMELINESS: rows appear about every {typical} day(s), but the newest is "
                    f"{newest.isoformat()} ({age} days ago, today is {today.isoformat()}) -- a gap far "
                    "larger than that cadence, so the MOST RECENT items are MISSING.", True)
        return (f"TIMELINESS: rows appear about every {typical} day(s) and the newest is {age} day(s) "
                "old -- within cadence, so the latest data is present.", False)
    stale = age > 120  # a single dated row: only flag a clearly-old lone item
    return (f"TIMELINESS: the only datable item is {newest.isoformat()} ({age} days ago)"
            + ("; likely missing more recent data." if stale else "; recent enough."), stale)
