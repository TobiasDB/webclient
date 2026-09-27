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
    """A ``date`` from a human/ISO/localised date string, or ``None``. Parsing is delegated to
    ``python-dateutil`` (a declared dependency, the same parser ``Field.date()`` uses) rather than a
    hand-rolled format list -- so ISO, RFC 822 (RSS ``pubDate``), month names, and localised forms
    (``31.12.2026`` DE dot, ``18/12/2025`` etc.) all parse without us enumerating each. ``dayfirst``
    stays False (US-style) for an AMBIGUOUS slash date, and ``fuzzy`` is off so a non-date string is
    rejected (``None``) instead of being coerced. A value that carries NO year is refused, so a bare
    number/day isn't silently completed to today's month/year."""
    from dateutil import parser as _du

    s = s.strip()
    if not s or not any(c.isdigit() for c in s):
        return None
    # dateutil fills a missing field from today's date, so a BARE number would become a date -- reject
    # a plain integer unless it is a plausible 4-digit YEAR (so "1990" parses, but "5"/"42" do not).
    if s.isdigit() and not (1000 <= int(s) <= 9999):
        return None
    # DOT-separated dates are the European convention and are DAY-first (31.12.2026, 01.03.2026 = 1 Mar);
    # SLASH dates are US month-first (12/18/2025). This separator cue is all dateutil needs to
    # disambiguate the day/month order -- everything else (ISO, RFC 822, month names) it handles itself.
    dayfirst = "." in s and "/" not in s
    try:
        return _du.parse(s, dayfirst=dayfirst, fuzzy=False).date()
    except (ValueError, OverflowError, TypeError):
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
