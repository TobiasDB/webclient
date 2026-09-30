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

from importlib.resources import files
from pathlib import Path

import yaml
from pydantic import BaseModel, JsonValue
from web.resolve import Flag


class Brief(BaseModel):
    """The onboarding spec, in three sections (see the module docstring). SHARED: ``goal`` +
    ``fields``/``descriptions``/``types`` (the schema). LOCATE: ``seeds``/``candidates``/``start_url``/
    ``search``/``look``/``ignore``/``max_pages``/``prefer_api``. AUTHOR: ``author_hint``/
    ``selectors``/``optional``/``download``. REVIEW: ``review_hint`` (brief-specific strictness).
    ``name``/``title`` identify it; ``exit_when`` is an advisory exit hint. The CORE (``goal`` +
    schema) is REQUIRED and rendered into every stage; every stage hint is OPTIONAL. ``review_hint``
    is a requirement, so it is given to the AUTHOR (author to satisfy it) AND the REVIEW (check it) --
    so a brief-specific rule (e.g. ir-events timeliness) is never hardcoded in the pipeline.
    """

    # ══ CORE (REQUIRED) -- what the dataset IS. Rendered into EVERY stage that needs it. ═════════
    goal: str = ""  # the dataset, free text (the markdown body fills this) -- REQUIRED
    fields: list[str] = []  # the record fields wanted -- Author extracts them; Locate finds them
    descriptions: dict[str, str] = {}  # field -> what it is (a `schema:` list fills all three)
    types: dict[str, str] = {}  # field -> its type (string / url / number / datetime / ...)

    # ══ STAGE HINTS (ALL OPTIONAL) -- steer one pipeline stage; empty => that stage's default. ═══
    # -- LOCATE / CRAWL: where to look --
    search: str = (
        ""  # a web-search qualifier -- used ONLY when no seed/candidate/start_url is given
    )
    seeds: list[str] = []  # known sources to crawl (an explicit source makes `search` irrelevant)
    candidates: list[str] = []  # evaluate EXACTLY these URLs (skip crawling)
    start_url: str = ""  # one known source to seed the crawl from
    look: list[str] = []  # NL "prefer pages like…" guide for the crawl frontier
    ignore: list[str] = []  # NL "avoid pages like…" guide for the crawl frontier
    max_pages: int = 15  # crawl page bound
    prefer_api: bool = True  # prefer a live XHR/data-API over the HTML page
    # -- AUTHOR: how to extract --
    author_hint: str = (
        ""  # NL guidance for the query author (e.g. suggested patterns for this dataset)
    )
    selectors: dict[str, str] = {}  # field -> css/JSON-path override
    optional: list[str] = []  # fields that may legitimately be absent (not required in the review)
    download: bool = False  # harvest the file(s) themselves, not parsed rows
    # -- REVIEW: how strict on the extracted sample --
    review_hint: str = ""  # NL brief-SPECIFIC strictness for the per-sample review (e.g. ir-events:
    #                        "require UPCOMING events, not only archived; upcoming dates must be
    #                        current"). Empty => the default (fields present, real values, on-entity).

    # ══ identity / advisory ══════════════════════════════════════════════════════════════════════
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
        if "schema" in front:  # a list of `path: {type, description}` items (or bare names)
            fields, descriptions, types = _schema(front["schema"])
            data.setdefault("fields", fields)
            data.setdefault("descriptions", descriptions)
            data.setdefault("types", types)
        return cls.model_validate(data)

    @classmethod
    def load(cls, path: str) -> "Brief":
        """Load a reusable Brief from a markdown file (see :meth:`from_markdown`)."""
        return cls.from_markdown(Path(path).read_text(encoding="utf-8"))

    @classmethod
    def resolve(cls, spec: "str | Brief") -> "Brief":
        """A Brief from any spec: a :class:`Brief` (as-is), a markdown FILE path, a PACKAGED name
        (``briefs/<name>.md`` -- ``-``/``_`` interchangeable), else a bare GOAL string. Shared by the
        CLI and the programmatic entries so ``locate("ir-events", ...)`` and ``locate(my_brief, ...)``
        both work."""
        if isinstance(spec, Brief):
            return spec
        if Path(spec).is_file():
            return cls.load(spec)
        for name in {spec, spec.replace("-", "_"), spec.replace("_", "-")}:
            res = files("web.onboard").joinpath(f"briefs/{name}.md")
            if res.is_file():
                return cls.from_markdown(res.read_text(encoding="utf-8"))
        return cls(goal=spec)  # not a file or a packaged name -> treat it as the goal

    def with_entity(self, entity: str) -> "Brief":
        """Fold a target ENTITY (a company/site) into the search qualifier: a ``{entity}`` placeholder
        is substituted, else the entity is prepended to ``search`` (or the goal). No-op without one.
        (An explicit source still makes search irrelevant, so the entity only matters when searching.)
        """
        if not entity:
            return self
        search = (
            self.search.replace("{entity}", entity)
            if "{entity}" in self.search
            else f"{entity} {self.search or self.goal}".strip()
        )
        return self.model_copy(update={"search": search})


def packaged_briefs() -> "list[str]":
    """The names of the briefs bundled with the package (``web/onboard/briefs/*.md``)."""
    try:
        root = files("web.onboard").joinpath("briefs")
        return sorted(p.name[:-3] for p in root.iterdir() if p.name.endswith(".md"))
    except (FileNotFoundError, ModuleNotFoundError):
        return []


#: back-compat aliases -- one spec, one file; Locate reads its find-slice, Author its shape-slice.
LocateBrief = Brief
DatasetBrief = Brief

#: file extensions a ``download`` brief harvests -- the downloadable-file kinds a page lists (as
#: opposed to :data:`Brief.download` HTML/XML/JSON that carry extractable rows). One source of truth
#: for both phases: Locate scores a page's download links, Author builds the harvest selector.
DOWNLOAD_EXTENSIONS = (
    ".pdf",
    ".pptx",
    ".ppt",
    ".xlsx",
    ".xls",
    ".csv",
    ".doc",
    ".docx",
    ".odt",
    ".ods",
    ".rtf",
    ".txt",
    ".zip",
)


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
    #: the transport profile that WORKED during Locate (``basic`` / ``basic_browser`` /
    #: ``full_browser``) -- so Author fetches with the KNOWN-good profile instead of re-running
    #: resolve's escalation discovery. Baked into the query root (``resolve(profile=...)``).
    profile: str = ""
    detail: dict[str, JsonValue] = {}


__all__ = [
    "Brief",
    "LocateBrief",
    "DatasetBrief",
    "Reference",
    "DOWNLOAD_EXTENSIONS",
    "packaged_briefs",
]
