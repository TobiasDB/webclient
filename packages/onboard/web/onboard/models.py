"""The typed value models the Locate/Author split flows through -- pure data, no behaviour.

  * :class:`Brief`     -- the full onboarding spec (loadable from a markdown file with YAML
    frontmatter): what dataset to FIND (goal / seeds / start_url / search / look / ignore) AND the
    SHAPE wanted (fields / descriptions / selectors / optional / hints / download). Locate reads its
    find-slice, Author its shape-slice.
  * :class:`Reference` -- Locate's output / Author's input: WHERE the dataset is, plus hints.

``LocateBrief`` / ``DatasetBrief`` are back-compat ALIASES of :class:`Brief` (one spec, one file).
Keeping these as small pydantic models (not ad-hoc dicts) is what lets Locate and Author be
independent, testable units.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, JsonValue


class Brief(BaseModel):
    """The onboarding spec. FIND slice (Locate): ``goal`` (free-text ask), ``seeds`` (skip web
    search), ``candidates`` (skip crawling -- evaluate exactly these), ``start_url`` (one known
    source to seed from), ``search`` (a web-search qualifier appended after a company name),
    ``look`` / ``ignore`` (natural-language page guides), ``max_pages`` (crawl bound),
    ``prefer_api`` (the XHR/data-API preference). SHAPE slice (Author): ``fields`` (record fields),
    ``descriptions`` (field -> what it is), ``selectors`` (field -> css/JSON-path override),
    ``optional`` (fields that may be absent), ``hints`` (structural guidance for the query author),
    ``download`` (the file(s) themselves, not parsed rows). ``name`` / ``title`` identify it;
    ``exit_when`` is an advisory clean-exit condition."""

    goal: str = ""
    # -- FIND (Locate) --
    seeds: list[str] = []
    candidates: list[str] = []
    start_url: str = ""
    search: str = ""
    look: list[str] = []
    ignore: list[str] = []
    max_pages: int = 40
    prefer_api: bool = True
    # -- SHAPE (Author) --
    fields: list[str] = []
    descriptions: dict[str, str] = {}
    selectors: dict[str, str] = {}
    optional: list[str] = []
    hints: str = ""
    download: bool = False
    # -- identity / advisory --
    name: str = ""
    title: str = ""
    exit_when: str = ""

    @classmethod
    def from_markdown(cls, text: str) -> "Brief":
        """Build a Brief from a markdown document with YAML frontmatter (``---`` fenced). Keys map
        to the fields above; a ``schema:`` list (``- path: description`` items, or bare field
        strings) fills ``fields`` + ``descriptions``; the markdown body is the ``goal`` when no
        ``goal`` / ``description`` key is given."""
        front, body = _parse_frontmatter(text)
        data: dict[str, object] = {k: v for k, v in front.items() if k in cls.model_fields}
        if "description" in front and "goal" not in data:  # webclient calls the ask `description`
            data["goal"] = front["description"]
        data.setdefault("goal", body.strip())
        if "schema" in front:  # a list of `path: desc` items (or bare field names)
            fields, descriptions = _schema(front["schema"])
            data.setdefault("fields", fields)
            data.setdefault("descriptions", descriptions)
        return cls.model_validate(data)

    @classmethod
    def load(cls, path: str) -> "Brief":
        """Load a reusable Brief from a markdown file (see :meth:`from_markdown`)."""
        return cls.from_markdown(Path(path).read_text(encoding="utf-8"))


#: back-compat aliases -- one spec, one file; Locate reads its find-slice, Author its shape-slice.
LocateBrief = Brief
DatasetBrief = Brief


def _parse_frontmatter(text: str) -> "tuple[dict[str, JsonValue], str]":
    """Split ``---``-fenced YAML frontmatter from the markdown body; ``({}, text)`` if none."""
    if text.lstrip().startswith("---"):
        rest = text.lstrip()[3:]
        front_text, sep, body = rest.partition("\n---")
        if sep:
            loaded = yaml.safe_load(front_text)
            return (loaded if isinstance(loaded, dict) else {}), body.lstrip("\n")
    return {}, text


def _schema(schema: JsonValue) -> "tuple[list[str], dict[str, str]]":
    """A ``schema`` frontmatter list -> (field paths, path->description). Each item is a bare field
    name (string) or a single-key ``{path: description}`` mapping."""
    fields: list[str] = []
    descriptions: dict[str, str] = {}
    for item in schema if isinstance(schema, list) else []:
        if isinstance(item, str):
            fields.append(item)
        elif isinstance(item, dict):
            for path, desc in item.items():
                fields.append(str(path))
                if desc:
                    descriptions[str(path)] = str(desc)
    return fields, descriptions


class Reference(BaseModel):
    """WHERE the located dataset is, plus the hints Author needs. ``url`` is the source to query
    -- the **XHR/data-API endpoint when one backs the page** (JSON beats HTML), else the page
    itself; ``page_url`` is always the page it was found on. ``kind`` is the sniffed kind of
    ``url``. ``flags``/``signals`` are the conclusions/evidence that fired (from web.resolve).
    ``record_selector`` is the suggested repeating-row ``select_all`` target; ``pagination`` is
    the pager remedy (``paginate`` / ``paginate:scroll`` / ``paginate:cursor``); ``needs_browser``
    means a static fetch won't build the DOM; ``api_endpoint`` is the discovered data-API (equals
    ``url`` when preferred). ``detail`` carries any extra evidence."""

    url: str
    kind: str = "html"
    page_url: str = ""
    flags: list[str] = []
    signals: list[str] = []
    record_selector: "str | None" = None
    pagination: "str | None" = None
    needs_browser: bool = False
    api_endpoint: "str | None" = None
    detail: dict[str, JsonValue] = {}


__all__ = ["Brief", "LocateBrief", "DatasetBrief", "Reference"]
