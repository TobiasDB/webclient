"""Small shared value models + helpers: the expected-rows RANGE grammar, the markdown
frontmatter / schema parsing a brief is loaded with, and a web-search hit."""

from __future__ import annotations

import re

import yaml
from pydantic import BaseModel, JsonValue
from web.resolve import Flag


def parse_range(spec: str) -> "tuple[int, int] | None":
    """A row-count expectation -> ``(low, high)``: ``"10-50"``, ``"~20"`` (half to double),
    ``">=5"`` / ``"5+"``, ``"<200"`` / ``"<=200"``, or a bare number (exactly, ±25%)."""
    text = spec.strip().replace(" ", "")
    if not text:
        return None
    big = 10**9
    m = re.fullmatch(r"(\d+)-(\d+)", text)
    if m:
        return int(m.group(1)), int(m.group(2))
    m = re.fullmatch(r"~(\d+)", text)
    if m:
        n = int(m.group(1))
        return max(1, n // 2), n * 2
    m = re.fullmatch(r"(?:>=|>)?(\d+)\+?", text)
    if m and (text.startswith((">", ">=")) or text.endswith("+")):
        return int(m.group(1)), big
    m = re.fullmatch(r"<=?(\d+)", text)
    if m:
        return 0, int(m.group(1)) - (0 if text.startswith("<=") else 1)
    m = re.fullmatch(r"(\d+)", text)
    if m:
        n = int(m.group(1))
        return max(1, n * 3 // 4), n * 5 // 4 + 1
    return None


def in_range(count: int, bounds: "tuple[int, int] | None") -> "str | None":
    """``None`` when ``count`` is within ``bounds`` (or there are none); else a short note saying how
    it is off -- a flexible guide for a log line or a model hint, never a veto."""
    if bounds is None:
        return None
    low, high = bounds
    if count < low:
        return f"{count} record(s) is BELOW the brief's expectation ({_show(bounds)})"
    if count > high:
        return f"{count} record(s) is ABOVE the brief's expectation ({_show(bounds)})"
    return None


def _show(bounds: "tuple[int, int]") -> str:
    low, high = bounds
    return f">= {low}" if high >= 10**9 else f"{low}-{high}"


def _parse_frontmatter(text: str) -> "tuple[dict[str, JsonValue], str]":
    """Split ``---``-fenced YAML frontmatter from the markdown body; ``({}, text)`` if none."""
    if text.lstrip().startswith("---"):
        rest = text.lstrip()[3:]
        front_text, sep, body = rest.partition("\n---")
        if sep:
            loaded = yaml.safe_load(front_text)
            return (loaded if isinstance(loaded, dict) else {}), body.lstrip("\n")
    return {}, text


def _schema(schema: JsonValue) -> "tuple[list[str], dict[str, str], dict[str, str]]":
    """A ``schema`` frontmatter list -> (field paths, path->description, path->type). Each item is a
    bare field name (string), a ``{path: description}`` mapping (description only), or a
    ``{path: {type: ..., description: ...}}`` mapping (the rich form: each field gets a type and a
    description)."""
    fields: list[str] = []
    descriptions: dict[str, str] = {}
    types: dict[str, str] = {}
    for item in schema if isinstance(schema, list) else []:
        if isinstance(item, str):
            fields.append(item)
        elif isinstance(item, dict):
            for path, spec in item.items():
                name = str(path)
                fields.append(name)
                if isinstance(spec, dict):  # {type: ..., description: ...}
                    if spec.get("description"):
                        descriptions[name] = str(spec["description"])
                    if spec.get("type"):
                        types[name] = str(spec["type"])
                elif spec:  # a bare description string
                    descriptions[name] = str(spec)
    return fields, descriptions, types


class SearchHit(BaseModel):
    """One web-search result: the URL plus the ``title`` / ``snippet`` the engine showed -- what the
    search step's verify filter judges a seed by (domain + title + snippet), not the URL alone."""

    url: str
    title: str = ""
    snippet: str = ""


__all__ = ["SearchHit", "in_range", "parse_range"]
