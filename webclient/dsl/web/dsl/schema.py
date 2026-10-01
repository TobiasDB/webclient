"""The OPTIONAL schema a plan may carry: what its rows mean. Fields (name, type, description,
optional, document -- a field whose value is a file URL the run fetches), the identity rule (the
parts a row's ``_identity`` is made of; empty = every extracted field), the expected row count
(``"10-50"`` / ``"~20"`` / ``">=5"``) and the cadence (how often a new row appears). A query
without one still runs; with one, a :class:`~web.dsl.report.Report` can judge the rows and a sink
knows the row schema. Pure data -- it rides the blob."""

from __future__ import annotations

import re

from pydantic import BaseModel

#: types whose VALUE is a downloadable document (the cell holds the file URL).
DOCUMENT_TYPES = frozenset({"document", "file", "blob", "pdf", "download", "attachment", "binary"})
#: types a run can parse as a date / datetime (a timeliness read).
DATE_TYPES = frozenset({"date", "datetime", "time"})


class FieldDef(BaseModel):
    name: str
    type: str = "string"
    description: str = ""
    optional: bool = False

    @property
    def document(self) -> bool:
        return self.type.lower() in DOCUMENT_TYPES

    @property
    def dated(self) -> bool:
        return self.type.lower() in DATE_TYPES


class Schema(BaseModel):
    fields: list[FieldDef] = []
    identity: list[str] = []  # the identity parts (a field name / a css); [] = every field
    expect_rows: str = ""  # "10-50" / "~20" / ">=5" / "<200" / "40"
    cadence: str = ""  # free text: "daily" / "a few a month" -- a timeliness hint

    @property
    def names(self) -> "list[str]":
        return [f.name for f in self.fields]

    @property
    def required(self) -> "list[str]":
        return [f.name for f in self.fields if not f.optional]

    @property
    def documents(self) -> "list[str]":
        return [f.name for f in self.fields if f.document]

    @property
    def dated(self) -> "list[str]":
        return [f.name for f in self.fields if f.dated]

    def types(self) -> "dict[str, str]":
        """field -> type, plus the identity column -- the row schema a sink receives."""
        out = {f.name: f.type or "string" for f in self.fields}
        out["_identity"] = "identity"
        return out

    def expected_range(self) -> "tuple[int, int] | None":
        return parse_range(self.expect_rows)


_RANGE = re.compile(r"^\s*(\d+)\s*-\s*(\d+)\s*$")
_APPROX = re.compile(r"^\s*~\s*(\d+)\s*$")
_AT_LEAST = re.compile(r"^\s*(?:>=\s*(\d+)|(\d+)\s*\+)\s*$")
_BELOW = re.compile(r"^\s*(<=?)\s*(\d+)\s*$")
_EXACT = re.compile(r"^\s*(\d+)\s*$")


def parse_range(spec: str) -> "tuple[int, int] | None":
    """An expected-rows spec as ``(low, high)``: ``10-50``; ``~20`` = half to double; ``>=5`` /
    ``5+``; ``<200`` / ``<=200``; a bare ``40`` = roughly 40 (three quarters to 1.25x). ``None``
    when empty or not a spec (counts are a guide, never a hard rule)."""
    if not spec:
        return None
    if m := _RANGE.match(spec):
        return int(m.group(1)), int(m.group(2))
    if m := _APPROX.match(spec):
        n = int(m.group(1))
        return n // 2, n * 2
    if m := _AT_LEAST.match(spec):
        return int(m.group(1) or m.group(2)), 10**9
    if m := _BELOW.match(spec):
        n = int(m.group(2))
        return 0, n if m.group(1) == "<=" else n - 1
    if m := _EXACT.match(spec):
        n = int(m.group(1))
        return (n * 3) // 4, (n * 5) // 4 + 1
    return None


def in_range(count: int, bounds: "tuple[int, int] | None") -> "str | None":
    """``None`` when ``count`` is within ``bounds`` (or there are none), else a one-line note."""
    if bounds is None:
        return None
    lo, hi = bounds
    if count < lo:
        return f"{count} row(s) is BELOW the expected {_show(bounds)}"
    if count > hi:
        return f"{count} row(s) is ABOVE the expected {_show(bounds)}"
    return None


def _show(bounds: "tuple[int, int]") -> str:
    lo, hi = bounds
    return f"at least {lo}" if hi >= 10**9 else f"{lo}-{hi}"


__all__ = ["DATE_TYPES", "DOCUMENT_TYPES", "FieldDef", "Schema", "in_range", "parse_range"]
