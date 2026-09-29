"""The typed value models the Locate/Author split flows through -- pure data, no behaviour.

  * :class:`Brief`     -- the ONE onboarding spec both phases read (loadable from a markdown file
    with YAML frontmatter). Its keys fall into three sections:

      SHARED (both phases)  ``goal`` (the dataset, free text -- the markdown BODY fills it) and the
                            ``schema`` = ``fields`` + ``descriptions``. Author EXTRACTS those fields;
                            Locate USES them to recognise the right dataset (a page showing them
                            scores higher). So the schema is not just an Author concern.
      LOCATE (find WHERE)   ``seeds`` / ``candidates`` / ``start_url`` (explicit sources), ``search``
                            (a web-search qualifier -- used ONLY when no explicit source is given, so
                            a reference URL / resolve options make ``search`` irrelevant), ``look`` /
                            ``ignore`` (page guides), ``max_pages``, ``prefer_api``.
      AUTHOR (how to EXTRACT) ``selectors`` (field -> css/JSON-path override), ``optional`` (fields
                            that may be absent), ``hints`` (structural guidance), ``download`` (the
                            file(s) themselves, not parsed rows).

  * :class:`Reference` -- Locate's output / Author's input: WHERE the dataset is, plus hints.

``LocateBrief`` / ``DatasetBrief`` are back-compat ALIASES of :class:`Brief` (one spec, one file):
Locate reads the SHARED + LOCATE keys, Author the SHARED + AUTHOR keys. Keeping this a small
pydantic model (not an ad-hoc dict) is what lets Locate and Author stay independent, testable units.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, JsonValue
from web.resolve import Flag


class Brief(BaseModel):
    """The onboarding spec, in three sections (see the module docstring). SHARED: ``goal`` +
    ``fields``/``descriptions`` (the schema). LOCATE: ``seeds``/``candidates``/``start_url``/
    ``search``/``look``/``ignore``/``max_pages``/``prefer_api``. AUTHOR: ``selectors``/``optional``/
    ``hints``/``download``. ``name``/``title`` identify it; ``exit_when`` is an advisory exit hint.
    """

    # ── SHARED (both Locate and Author read these) ──────────────────────────────────────────────
    goal: str = ""  # the dataset, free text (the markdown body fills this)
    fields: list[str] = []  # the record fields wanted -- Author extracts them; Locate finds them
    descriptions: dict[str, str] = {}  # field -> what it is (a `schema:` list fills both)

    # ── LOCATE (find WHERE the dataset is) ──────────────────────────────────────────────────────
    seeds: list[str] = []  # known sources to crawl (an explicit source makes `search` irrelevant)
    candidates: list[str] = []  # evaluate EXACTLY these URLs (skip crawling)
    start_url: str = ""  # one known source to seed the crawl from
    search: str = (
        ""  # a web-search qualifier -- used ONLY when no seed/candidate/start_url is given
    )
    look: list[str] = []  # natural-language "prefer" page guide
    ignore: list[str] = []  # natural-language "avoid" page guide
    max_pages: int = 20  # crawl page bound
    prefer_api: bool = True  # prefer a live XHR/data-API over the HTML page

    # ── AUTHOR (how to EXTRACT it) ──────────────────────────────────────────────────────────────
    selectors: dict[str, str] = {}  # field -> css/JSON-path override
    optional: list[str] = []  # fields that may legitimately be absent
    hints: str = ""  # structural guidance for the query author
    download: bool = False  # harvest the file(s) themselves, not parsed rows

    # ── identity / advisory ─────────────────────────────────────────────────────────────────────
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
    ``url``. ``flags``/``signals`` are the conclusion/evidence NAMES that fired (a quick membership
    check); ``assessment`` is the FULL detection report -- each :class:`~web.resolve.Flag` with its
    description, confidence, and the signals (with confidences) that triggered it, so a caller can
    see WHY a conclusion fired. ``record_selector`` is the suggested repeating-row ``select_all``
    target; ``pagination`` is the pager remedy (``paginate`` / ``paginate:scroll`` /
    ``paginate:cursor``); ``needs_browser`` means a static fetch won't build the DOM; ``api_endpoint``
    is the discovered data-API (equals ``url`` when preferred). ``detail`` carries any extra
    evidence (e.g. the dataset-likeness ``score``)."""

    url: str
    kind: str = "html"
    page_url: str = ""
    flags: list[str] = []
    signals: list[str] = []
    assessment: list[Flag] = []
    record_selector: "str | None" = None
    pagination: "str | None" = None
    needs_browser: bool = False
    api_endpoint: "str | None" = None
    detail: dict[str, JsonValue] = {}


__all__ = ["Brief", "LocateBrief", "DatasetBrief", "Reference"]
