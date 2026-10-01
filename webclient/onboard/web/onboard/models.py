"""Small shared helpers: the markdown frontmatter / schema parsing a brief is loaded with, and a
web-search hit."""

from __future__ import annotations

import yaml
from pydantic import BaseModel, JsonValue
from web.resolve import Flag


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


__all__ = ["SearchHit"]
