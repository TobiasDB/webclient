"""What a run REPORTS, mechanically, alongside its rows: the count against the schema's expected
range, per-field fill rates, duplicate identities, the newest row's age (dated fields) against the
cadence, per-row issues (lenient runs), every fetch made, and failures. The first thing a review
reads -- before any model is asked."""

from __future__ import annotations

import datetime as _dt

from pydantic import BaseModel, JsonValue

from .identity import IDENTITY_COLUMN
from .schema import Schema, in_range
from .values import parse_when

ISSUES_COLUMN = "_issues"


class Fetch(BaseModel):
    url: str
    status: int = 0
    elapsed: float = 0.0
    tier: int = 0  # 0 = the base tier; n = the n-th escalation rung


class Issue(BaseModel):
    row: int
    field: str = ""
    message: str


class Report(BaseModel):
    rows: int = 0
    expected: str = ""  # "" = within the expected range (or none declared), else the note
    fill: dict[str, float] = {}  # field -> fraction of rows that carry a value
    duplicates: int = 0  # rows sharing an _identity with an earlier row
    newest: str = ""  # the newest dated value seen (ISO), "" when none parsed
    newest_age_days: "float | None" = None
    issues: list[Issue] = []
    fetches: list[Fetch] = []
    failures: list[str] = []
    elapsed_s: float = 0.0

    @property
    def ok(self) -> bool:
        """No failure, rows present, no required field empty on every row."""
        return not self.failures and self.rows > 0 and all(v > 0 for v in self.fill.values())

    def summary(self) -> str:
        parts = [f"{self.rows} row(s)"]
        if self.expected:
            parts.append(self.expected)
        empty = [f for f, v in self.fill.items() if v == 0]
        if empty:
            parts.append("empty: " + ", ".join(empty))
        partial = [f"{f} {v:.0%}" for f, v in self.fill.items() if 0 < v < 1]
        if partial:
            parts.append("partial: " + ", ".join(partial))
        if self.duplicates:
            parts.append(f"{self.duplicates} duplicate(s)")
        if self.newest_age_days is not None:
            parts.append(f"newest {self.newest_age_days:.0f} day(s) old")
        if self.issues:
            parts.append(f"{len(self.issues)} row issue(s)")
        if self.failures:
            parts.append("FAILED: " + "; ".join(self.failures))
        parts.append(f"{len(self.fetches)} fetch(es), {self.elapsed_s:.1f}s")
        return "; ".join(parts)


def _empty(v: object) -> bool:
    return (
        v is None
        or (isinstance(v, str) and not v.strip())
        or (isinstance(v, (list, dict)) and not v)
    )


def assess(rows: "list[JsonValue]", schema: "Schema | None") -> Report:
    """The row-derived part of a report: count vs the expected range, fill rates (every field the
    schema names, else every key seen), duplicates, the newest dated value."""
    dicts = [r for r in rows if isinstance(r, dict)]
    names = (
        schema.names
        if schema is not None and schema.fields
        else sorted({k for r in dicts for k in r if not k.startswith("_")})
    )
    fill = {
        n: (sum(1 for r in dicts if not _empty(r.get(n))) / len(dicts) if dicts else 0.0)
        for n in names
    }
    seen: set[str] = set()
    dups = 0
    for r in dicts:
        ident = r.get(IDENTITY_COLUMN)
        if isinstance(ident, str):
            if ident in seen:
                dups += 1
            seen.add(ident)
    newest, age = "", None
    for n in schema.dated if schema is not None else []:
        for r in dicts:
            val = r.get(n)
            when = parse_when(val) if isinstance(val, str) else None
            if when is not None and (not newest or when.isoformat() > newest):
                newest = when.isoformat()
                today = _dt.datetime.now(when.tzinfo) if when.tzinfo else _dt.datetime.now()
                age = max(0.0, (today - when).total_seconds() / 86400)
    expected = in_range(len(rows), schema.expected_range()) if schema is not None else None
    return Report(
        rows=len(rows),
        expected=expected or "",
        fill=fill,
        duplicates=dups,
        newest=newest,
        newest_age_days=round(age, 1) if age is not None else None,
    )


__all__ = ["ISSUES_COLUMN", "Fetch", "Issue", "Report", "assess"]
