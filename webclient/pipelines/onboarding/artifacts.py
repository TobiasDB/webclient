"""onboarding.artifacts -- see the package docstring."""


import abc
import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Callable, Literal, Sequence, cast

from pydantic import BaseModel, PrivateAttr, model_validator

#: the pipeline's logger. Stages log progress here (seeds, crawl, candidates, the
#: evaluation, the query, spend); a CLI or app sets the level / handler. Each line is
#: also appended to ``OnboardingResult.steps`` for a programmatic trace.

from ...core.crawl import from_picks
from ...core.document.models import Flag, PaginationHint
from ...policy import (
    AntiBotPolicy,
    BrowserPolicy,
    ProxyPolicy,
    Resolve,
)
from ...llm.guides import lazy_query_guide
from ...query.expr import from_blob
from ...interface import Reference, WebClient, wq
from ...clients.llm import Budget, BudgetExceeded, LlmClient, LlmError
from ...llm.prompts import render_prompt

from .common import LLM, log


# --------------------------------------------------------------------------- #
# Artifacts (the typed things that flow between stages).
# --------------------------------------------------------------------------- #


def _parse_frontmatter(text: str) -> "tuple[dict[str, Any], str]":
    """Split a ``---`` YAML frontmatter block from the markdown body and parse it with
    ``yaml.safe_load``. Returns ``(front, body)``; no frontmatter -> ``({}, text)``."""
    import yaml

    if not text.lstrip().startswith("---"):
        return {}, text
    rest = text.lstrip()[3:].lstrip("\n")
    end = rest.find("\n---")
    if end == -1:
        return {}, text
    block, body = rest[:end], rest[end + 4 :].lstrip("\n")
    front = yaml.safe_load(block) or {}
    return (front if isinstance(front, dict) else {}), body


def _strip_optional(path: str) -> "tuple[str, bool]":
    """Split a schema path into ``(clean_path, is_optional)``: a trailing ``?`` marks the
    field optional (``sku?`` / ``price.discount?``) -- it MAY be absent on a page, and the
    author should not force it. Returns the path without the ``?`` and whether it was set."""
    path = path.strip()
    if path.endswith("?"):
        return path[:-1].strip(), True
    return path, False


def _parse_schema(schema: Any) -> "tuple[list[str], dict[str, str], list[str]]":
    """Interpret a frontmatter ``schema`` into ``(fields, descriptions, optional)``. Each
    item is a dotted field path, either a bare string (``name``) or a one-key mapping
    carrying its description (``{name: the display name}``); dotted paths nest, and a
    trailing ``?`` on a path marks that field OPTIONAL (``sku?``)."""
    fields: list[str] = []
    descriptions: dict[str, str] = {}
    optional: list[str] = []
    for item in schema if isinstance(schema, list) else ([schema] if schema else []):
        if isinstance(item, dict):
            for raw, desc in item.items():
                path, is_opt = _strip_optional(str(raw))
                if path:
                    fields.append(path)
                    if is_opt:
                        optional.append(path)
                    if desc:
                        descriptions[path] = str(desc).strip()
        elif item:
            path, is_opt = _strip_optional(str(item))
            if path:
                fields.append(path)
                if is_opt:
                    optional.append(path)
    return fields, descriptions, optional


class SchemaField(BaseModel):
    """One field in the target schema: a ``name``, an optional ``description`` (what it
    is / how to fill it), and nested ``children`` for structured values (a ``price``
    with ``value`` / ``unit`` / ``modifiers``)."""

    name: str
    description: str = ""
    optional: bool = False  # may be absent on a page -- don't force it (use optional=True)
    children: "list[SchemaField]" = []


class Brief(BaseModel):
    """What dataset we want to onboard -- a reusable spec, loadable from a markdown file
    with YAML frontmatter (:meth:`from_markdown` / :meth:`load`). ``description`` is the
    free-text ask (the markdown body); the ``schema`` (``fields`` + per-field
    ``descriptions``, nested via dotted paths) is what each record should carry;
    ``look`` / ``ignore`` are NATURAL-LANGUAGE guides (what kind of pages to head for /
    skip) the model interprets; ``crawl`` overrides the pipeline's crawl knobs
    (``max_pages`` / ``depth`` / ``rounds`` / ``browser``); ``name`` / ``title``
    identify the brief."""

    description: str = ""
    fields: list[str] = []  # the target schema's leaf/branch paths (dotted for nesting)
    descriptions: dict[str, str] = {}  # path -> what that field is / how to fill it
    optional: list[str] = []  # field paths that MAY be absent on a page (mark with a `?`)
    name: str = ""  # a short slug id (e.g. "product-catalogue")
    title: str = ""  # a human title
    search: str = ""  # deterministic web-search qualifier: "<company> <search>" (e.g.
    # "investor relations news"); a literal "{company}" in it is substituted instead of prepended
    look: list[str] = []  # natural-language guides: what kinds of pages to head for
    ignore: list[str] = []  # natural-language guides: what kinds of pages to skip
    #: a brief-level EXIT CONDITION -- a natural-language check evaluated on the chosen source;
    #: when it holds the pipeline stops CLEANLY (a distinct exit, not a failure) instead of
    #: authoring a query. For a dataset we can't reliably capture in some page state, e.g.
    #: ir-events: "the upcoming-events section is empty -- its structure is unknown".
    exit_when: str = ""
    #: brief-specific STRUCTURAL guidance for the query author -- how this dataset is laid out
    #: on the page, threaded straight into the write_query prompt. This is where a brief passes
    #: down what it knows about a hard-to-see shape, e.g. ir-events: "the dataset splits into
    #: UPCOMING and ARCHIVED sections; ARCHIVED is often tabbed by year; the UPCOMING section
    #: may be a SINGLE row in a DIFFERENT format from the archived rows (or empty) -- capture it
    #: too, don't assume one selector fits every record."
    hints: str = ""
    crawl: dict[str, Any] = {}  # pipeline crawl overrides (max_pages/depth/rounds/browser)

    @model_validator(mode="after")
    def _normalize_optional(self) -> "Brief":
        """Accept a trailing ``?`` on directly-passed ``fields`` too (``fields=["name",
        "sku?"]``): strip it and record the field as optional. So both a loaded brief and
        a hand-built one express optionality the same way."""
        clean: list[str] = []
        opt = list(self.optional)
        for f in self.fields:
            name, is_opt = _strip_optional(f)
            clean.append(name)
            if is_opt and name not in opt:
                opt.append(name)
        self.fields = clean
        self.optional = opt
        return self

    @classmethod
    def from_markdown(cls, text: str) -> "Brief":
        """Build a :class:`Brief` from a markdown document with YAML frontmatter. Keys:
        ``name`` / ``title``; ``schema`` (a list of ``path: description`` items -- dotted
        paths nest, the text is that field's description; a bare string is a field with
        no description); ``search`` (a deterministic web-search qualifier appended after the
        company name, e.g. ``investor relations news``); ``look`` / ``ignore`` (NL guide
        lines); ``hints`` (brief-specific STRUCTURAL guidance for the query author -- how the
        dataset is laid out on the page); ``exit_when`` (a natural-language exit condition);
        ``crawl`` (a mapping of pipeline crawl overrides -- ``max_pages`` / ``depth``
        / ``rounds`` / ``browser``); ``description`` (else the body)."""
        front, body = _parse_frontmatter(text)

        def as_list(v: Any) -> list[str]:
            return [str(x) for x in v] if isinstance(v, list) else ([str(v)] if v else [])

        fields, descriptions, optional = _parse_schema(front.get("schema") or front.get("fields"))
        crawl = front.get("crawl")
        return cls(
            description=str(front.get("description") or body).strip(),
            fields=fields,
            descriptions=descriptions,
            optional=optional,
            name=str(front.get("name") or ""),
            title=str(front.get("title") or ""),
            search=str(front.get("search") or ""),
            exit_when=str(front.get("exit_when") or ""),
            hints=str(front.get("hints") or ""),
            look=as_list(front.get("look")),
            ignore=as_list(front.get("ignore")),
            crawl=crawl if isinstance(crawl, dict) else {},
        )

    @classmethod
    def load(cls, path: str) -> "Brief":
        """Load a reusable brief from a markdown file (see :meth:`from_markdown`)."""
        from pathlib import Path

        return cls.from_markdown(Path(path).read_text(encoding="utf-8"))

    def schema_tree(self) -> "list[SchemaField]":
        """The target schema as a nested :class:`SchemaField` tree, built from the dotted
        ``fields`` + their ``descriptions`` -- so ``["name", "price.value",
        "price.unit"]`` with a description on ``price`` becomes ``name`` and a ``price``
        node (its description) with ``value`` / ``unit`` children. The query author nests
        a sub-``extract`` per branch so the output JSON mirrors this shape."""
        roots: list[SchemaField] = []
        index: dict[str, SchemaField] = {}  # full path -> node
        optional = set(self.optional)
        for path in self.fields:
            parent = ""
            for part in (p.strip() for p in path.split(".") if p.strip()):
                full = f"{parent}.{part}" if parent else part
                node = index.get(full)
                if node is None:
                    node = SchemaField(
                        name=part,
                        description=self.descriptions.get(full, ""),
                        optional=full in optional,
                    )
                    index[full] = node
                    (index[parent].children if parent else roots).append(node)
                parent = full
        return roots

    def field_tree(self) -> "dict[str, Any]":
        """The target schema as a plain nested name tree (no descriptions):
        ``["name", "price.value"]`` -> ``{"name": {}, "price": {"value": {}}}``."""
        tree: dict[str, Any] = {}
        for f in self.fields:
            node = tree
            for part in (p.strip() for p in f.split(".") if p.strip()):
                node = node.setdefault(part, {})
        return tree

    @property
    def is_nested(self) -> bool:
        return any("." in f for f in self.fields)


class SearchHit(BaseModel):
    url: str
    title: str = ""
    snippet: str = ""


class Seed(BaseModel):
    url: str
    title: str = ""
    why: str = ""  # why this seed might reach the dataset


class Candidate(BaseModel):
    """A crawled page in the running to hold the dataset."""

    url: str
    kind: str = "page"  # "api" | "page" | "spa"
    tier: str = "could"  # "must" | "should" | "could" evaluate
    note: str = ""


class CandidateEval(BaseModel):
    """The model's read of whether a candidate is the source to scrape."""

    url: str
    dataset_present: bool = False
    is_queryable: bool = False  # an API / endpoint that serves the WHOLE dataset
    sort_order: str | None = None  # "newest-first" | "oldest-first" | "unsorted" (from the dates)
    recency_hint: str = ""  # where the MOST RECENT records are (a tab/filter/first page), for write_query
    completeness: str | None = None  # e.g. "full" | "partial" | "unknown"
    has_pagination: bool = False
    #: the detected pagination hint (the ways it could be paged, best first + totals), so write_query
    #: bakes the pager its best mode describes -- None when unpaged.
    pagination_hint: PaginationHint | None = None
    has_filters: bool = False
    dataset_is_subset: bool = False  # our brief is a subset of what's on offer
    mostly_unstructured: bool = False
    drilldown_links: bool = False
    #: this page DOCUMENTS an API (developer docs / reference / OpenAPI) rather than
    #: being the data -- never a scrapable source, however "API-ish" it looks.
    is_api_docs: bool = False
    scrapability: int = 0  # 0-10; higher is easier/cleaner to scrape
    verdict: str = ""  # the model's one-line reason for its judgement (logged)
    #: the detected flags on the page (name -> confidence), and, when the SPA is
    #: backed by a same-origin data API, the endpoint to query INSTEAD of scraping.
    flags: dict[str, float] = {}
    #: the EVIDENCE behind each present flag -- the signals that fired, as readable
    #: "name (stage, confidence): reason" strings, so a flag can be justified.
    flag_signals: dict[str, list[str]] = {}
    api_endpoint: str | None = None
    #: the dataset is reached only through interaction (forms / buttons), so a static
    #: fetch or a single query will not surface it -- a browser session is needed.
    interactive: bool = False
    #: the brief's EXIT CONDITION (``Brief.exit_when``) evaluated on this page: when True the
    #: pipeline stops cleanly (a defined exit, not a failure) instead of authoring a query.
    exit_when_met: bool = False
    exit_reason: str = ""  # the one-line reason, when exit_when_met
    #: the page could NOT be assessed because the MODEL was unavailable (the LLM call errored --
    #: rate limit / quota / transport), NOT because the page was judged to lack a dataset. Keeps a
    #: transient model outage from being reported as "no data here" (a retry, not a dead source).
    llm_unavailable: bool = False

    @property
    def usable(self) -> bool:
        return self.dataset_present and self.scrapability >= 5


class QueryPart(BaseModel):
    """One SECTION sub-query of a split dataset. When a dataset is spread across
    differently-shaped sections on a page (an UPCOMING callout + an ARCHIVED list), the model
    writes one simple ``wq.doc...project()`` per section and the pipeline runs each and
    CONCATENATES the rows (see :func:`run_query`). Each part is a self-contained, runnable
    blob in its own right; a plain single-section query has no parts."""

    blob: str  # the portable, self-contained blob for this section (from_blob-rebuildable)
    describe: str  # a readable one-line rendering of this section's chain
    explain: str = ""  # this section's SQL-EXPLAIN step tree
    row_count: int = 0  # how many rows this section produced when tested


class QueryArtifact(BaseModel):
    """The authored lazy query, ready to reload and run. ``blob`` rebuilds it with
    ``from_blob`` and is SELF-CONTAINED: it bakes in the reference + the FULL fetch policy
    (browser tier AND proxy/antibot for an anti-bot source), so ``from_blob(blob).collect()``
    re-fetches under the same policy it was authored with. ``plan`` is the same chain as a
    plan dict (``from_plan``-loadable / the wire form). ``tested`` reports that the EXTRACTION
    ran against the source fetched at authoring time (the shipped blob is verified end-to-end
    by the pipeline's tests)."""

    blob: str  # the portable lazy-query blob (rebuildable with from_blob)
    describe: str  # a readable one-line rendering of the chain
    explain: str = ""  # the SQL-EXPLAIN-style step tree (computed at authoring, before testing)
    plan: dict[str, Any] = {}  # the plan dict (from_plan-loadable; the wire form)
    tested: bool = False  # did the EXTRACTION run against the fetched source without error?
    complete: bool = False  # tested + rows have content + every REQUIRED field populated
    row_count: int = 0  # how many rows it produced when tested
    sample: list[Any] = []  # up to 5 produced rows (as data), shown as a table
    #: the FULL fetch policy (browser/proxy/antibot) for this source, serialised. The blob
    #: bakes only the browser tier; a source needing proxy/antibot must be re-fetched with
    #: this policy (the blob alone would re-fetch un-proxied and get blocked).
    resolve: dict[str, Any] = {}
    #: the source URLs this one query runs against, unioned. Usually one, but a dataset
    #: split across distinct URLs (e.g. /products/cloud + /products/onprem -- NOT
    #: pagination) lists them all; :func:`run_query` resolves the query per base and
    #: concatenates the rows.
    base_urls: list[str] = []
    #: a TIMELINESS flag for the human review: the note from :func:`_timeliness` over ALL
    #: extracted rows (e.g. "newest 2026-09-14, within cadence" / "the most recent data is
    #: missing"), and whether it read as stale. Informational only -- a stale flag never
    #: blocks a working query from shipping.
    timeliness: str = ""
    stale: bool = False
    #: which dataset-query this artifact IS: ``"latest"`` (A -- the newest rows, page one, no
    #: backfill), ``"all"`` (B -- the whole dataset, pagination walked), ``"single"`` (an unpaged
    #: source, where latest == all), or ``""`` (mode not assigned). A "latest" and an "all" query
    #: are authored per source; both hang off :class:`OnboardingResult`.
    mode: str = ""
    #: COMPLETENESS (from the dataset shape): does this query cover the WHOLE dataset -- pagination
    #: walked, no active filter narrowing it? ``covers_all`` is the verdict; the note explains.
    completeness: str = ""
    covers_all: bool = True
    #: CORRECTNESS (from the dataset shape): is the captured set the right one -- unfiltered, with a
    #: known order? ``correct`` is False when an active filter makes the visible rows a subset.
    correctness: str = ""
    correct: bool = True
    #: why each EARLIER authoring attempt was rejected (one short line each, in order), so the
    #: onboard output shows the path to this query -- empty when the first attempt succeeded.
    attempts: list[str] = []
    #: required field(s) the model could NOT populate from the source across repeated, targeted
    #: retries -- i.e. genuinely ABSENT from the page (the records and the OTHER fields extracted
    #: cleanly). Set when authoring stops early on a persistently-empty field instead of burning
    #: every retry; names exactly what a human must add to the source or drop from the brief.
    absent: list[str] = []
    #: the SECTION sub-queries of a split dataset, run and CONCATENATED by :func:`run_query`
    #: into one flat result. ``[]`` for a plain single-section query (the common case, run via
    #: ``blob``); length >= 2 for a concat-join, where ``blob``/``describe``/``explain`` above
    #: describe the FIRST section and ``row_count``/``sample``/``complete``/``timeliness`` are
    #: the COMBINED verdict over every section's rows.
    parts: list[QueryPart] = []
    #: the companion "latest" (A) query, present only when THIS artifact is the "all" (B, paginated)
    #: query for a paginated source: the same extraction WITHOUT the backfill pager -- page one, the
    #: newest rows, for a cheap incremental poll. ``None`` for an unpaged source (latest == all).
    latest: "QueryArtifact | None" = None


class Review(BaseModel):
    """One LLM judgement of a pipeline stage's choices -- the meta-review that grades the
    run so a human sees where it went right or wrong. ``stage`` is which review this is
    (``crawl`` / ``select`` / ``query`` / ``failure``); ``verdict`` is a one-word grade
    (``good`` / ``partial`` / ``poor``, or ``diagnosis`` for a failure); ``score`` is 0-10;
    ``issues`` are the concrete problems found; ``summary`` is the one-line human takeaway."""

    stage: str
    verdict: str = ""
    passed: bool = True  # did this stage's result pass the review? a fail GATES the pipeline
    score: int = 0
    issues: list[str] = []
    summary: str = ""


class OnboardingResult(BaseModel):
    """The end product for one company: the chosen source + how to fetch and query it."""

    model_config = {"arbitrary_types_allowed": True}

    company: str
    brief: Brief
    ok: bool = False
    exited: bool = False  # stopped cleanly by the brief's exit_when condition (NOT a failure)
    reason: str = ""  # why, when not ok
    evaluation: CandidateEval | None = None
    reference: Any = None  # the lazy Reference (a core; not re-validated by pydantic)
    resolve: Resolve | None = None
    query: QueryArtifact | None = None  # the primary shipped query (the "all" one when paginated)
    #: the two dataset queries authored for the source: ``query_latest`` (A -- the newest rows, a
    #: cheap incremental poll) and ``query_all`` (B -- the whole dataset, pagination walked). For an
    #: unpaged source the two are the same query. Both default to ``query`` when only one was built.
    query_latest: QueryArtifact | None = None
    query_all: QueryArtifact | None = None
    #: the per-stage products, so the run can be VISUALISED and debugged step by step: the search
    #: seeds, the crawled pages (url/title/tier/flags), and the ranked candidates. Filled by
    #: onboard_company from the run's artifacts (best-effort; empty when a stage didn't run).
    seeds: list[str] = []
    crawl_pages: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    steps: list[str] = []  # a human-readable trace of the run (also logged)
    reviews: list[Review] = []  # LLM meta-reviews grading the run's choices (opt-in)
    cost_usd: float = 0.0  # LLM spend for this company (when an LlmClient was used)
    #: the checkpoint an ``interactive`` run is waiting at (an ``Ask``), else ``None``;
    #: answer it with :meth:`resume`.
    pending: Any = None
    _resume: Any = PrivateAttr(default=None)  # the pipeline's resume hook (set by onboard_company)

    def resume(self, answer: Any) -> "OnboardingResult":
        """Continue a run that paused at a checkpoint (``pending``) with ``answer`` (``"yes"`` /
        ``"no"`` at the confirm gate). Raises if nothing is pending."""
        if self.pending is None or self._resume is None:
            raise RuntimeError("this onboarding run is not waiting at a checkpoint")
        return cast("OnboardingResult", self._resume(answer))


@dataclass
class _RunArtifacts:
    """The live intermediate products of one run, kept so the review stage can judge each
    stage's choices (they are not on the serialisable :class:`OnboardingResult`)."""

    seeds: "list[Seed]" = field(default_factory=list)
    crawl: Any = None            # the finished Crawl (pages / failures / frontier)
    candidates: "list[Candidate]" = field(default_factory=list)
    query_doc: Any = None        # the fetched source Document (for the query review skeleton)


#: chatty third-party loggers to hush when WE own the logging setup, so the pipeline's
#: progress isn't buried under each httpx request line etc.
_NOISY_LOGGERS = ("httpx", "httpcore", "urllib3", "playwright", "asyncio", "werkzeug")


def _ensure_logging() -> None:
    """Make the pipeline's progress ALWAYS visible: if nothing has configured logging
    (no handler on our logger or the root), attach a plain stderr handler at INFO and
    hush the chatty transport loggers (httpx/httpcore/...). A host that has set up
    logging keeps full control -- we add nothing then."""
    if log.handlers or logging.getLogger().handlers:
        return
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    log.propagate = False
    for name in _NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)


def _trace(result: OnboardingResult, message: str, *args: Any) -> None:
    """Emit one pipeline step -- always printed (see :func:`_ensure_logging`) -- and append it to
    the result's ``steps`` trace. The running LLM spend is shown as a prefix ONLY once it is
    non-zero (so an untracked/free run isn't cluttered with ``[$0.0000]`` on every line)."""
    rendered = message % args if args else message
    result.steps.append(rendered)
    prefix = f"[${result.cost_usd:.4f}] " if result.cost_usd else ""
    log.info("%s%s: %s", prefix, result.company, rendered)


def _cell(value: Any) -> str:
    """One table cell -- JSON for a nested value, whitespace COLLAPSED for a string (a newline
    in a value, e.g. an RSS description, would otherwise break the table's alignment so later
    columns look empty), truncated so the table stays legible."""
    s = (json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list))
         else " ".join(str(value).split()))
    return s if len(s) <= 40 else s[:39] + "…"


def _render_table(rows: "list[Any]", max_rows: int = 5) -> "list[str]":
    """The sample output rows as an aligned text table -- columns are the row keys (a
    non-dict row falls back to a single ``value`` column)."""
    rows = list(rows[:max_rows])
    if not rows:
        return ["    (no rows)"]
    dicts = [r if isinstance(r, dict) else {"value": r} for r in rows]
    cols: list[str] = []
    for d in dicts:
        cols += [k for k in d if k not in cols]
    width = {c: max([len(c)] + [len(_cell(d.get(c, ""))) for d in dicts]) for c in cols}

    def row(vals: "list[str]") -> str:
        return "    " + "  ".join(f"{v:<{width[c]}}" for c, v in zip(cols, vals))

    out = [row(cols), row(["-" * width[c] for c in cols])]
    out += [row([_cell(d.get(c, "")) for c in cols]) for d in dicts]
    return out


def _resolve_summary(resolve: "Resolve | None") -> str:
    """The resolve policy in a compact, reproducible form (only the active concerns)."""
    if resolve is None:
        return "none — a plain static fetch"
    parts: list[str] = []
    if resolve.browser is not None:
        parts.append(f"browser={resolve.browser.when}" + (", stealth" if resolve.browser.stealth else ""))
    if resolve.proxy is not None:
        parts.append("proxy=on")
    if resolve.antibot is not None:
        parts.append(f"antibot={resolve.antibot.level}")
    return ", ".join(parts) if parts else "retry/rate defaults only"


def _summarize(result: OnboardingResult) -> None:
    """The always-printed end-of-run summary: outcome, the chosen source with its scores
    + flags, the reference + resolve args to reproduce the fetch, the authored query with
    a sample-output table + its portable blob, and the spend. Enough to re-run by hand."""
    ref_url = str(getattr(result.reference, "url", "")) or (
        result.evaluation.url if result.evaluation else ""
    )
    lines = ["", f"── {result.company} " + "─" * max(3, 46 - len(result.company))]
    outcome = ("ready" if result.ok else
               f"exited (brief condition) — {result.reason}" if result.exited else
               "not onboarded — " + result.reason)
    lines.append(f"  result:    {outcome}")
    ev = result.evaluation
    if ev is not None:
        lines.append(f"  source:    {ev.url}")
        if ev.api_endpoint:  # the JSON API behind the page (from XHR correlation) -- query it, not the DOM
            lines.append(f"  data API:  {ev.api_endpoint}  ← the JSON behind the page (query this, not the DOM)")
        lines.append(
            "  scores:    "
            + f"scrapability {ev.scrapability}/10, queryable={ev.is_queryable}, "
            + f"present={ev.dataset_present}, complete={ev.completeness or '?'}, "
            + f"paginated={ev.has_pagination}, filters={ev.has_filters}, "
            + f"subset={ev.dataset_is_subset}, interactive={ev.interactive}, "
            + f"sort={ev.sort_order or '?'}"
        )
        if ev.recency_hint:  # where the most recent records are (fed to the query writer)
            lines.append(f"  recency:   {ev.recency_hint}")
        if ev.flags:  # each present flag with the SIGNALS (evidence) behind it
            lines.append("  flags:")
            for name, conf in sorted(ev.flags.items(), key=lambda x: -x[1]):
                lines.append(f"    {name} ({conf:.2f})")
                for sig in ev.flag_signals.get(name, []):
                    lines.append(f"      · {sig}")
        if ev.verdict:  # the model's reason for choosing this source
            lines.append(f"  reason:    {ev.verdict}")
    # the reference(s) the query actually runs against (== the query's own root now)
    refs = result.query.base_urls if (result.query and result.query.base_urls) else (
        [ref_url] if ref_url else []
    )
    if refs:
        lines.append(f"  reference: {', '.join(refs)}")
    lines.append(f"  resolve:   {_resolve_summary(result.resolve)}")
    if result.query is not None:
        q = result.query
        split = len(q.parts) > 1  # a concat-join of per-section sub-queries
        lines.append(f"  query:     {q.describe}"
                     + (f"   (split: {len(q.parts)} sections, rows concatenated)" if split else ""))
        if q.explain:  # the visual step tree of the (valid) query (the first section, when split)
            lines.append("  explain:" + ("   (section 1)" if split else ""))
            lines += [f"    {ln}" for ln in q.explain.splitlines()]
        lines.append(f"  tested:    {'✓' if q.tested else '✗'}  {q.row_count} row(s)"
                     + (" combined" if split else ""))
        if q.timeliness:  # the TIMELINESS flag: is the newest extracted row recent? (a flag, not a gate)
            tabbed = ev is not None and "tabbed" in (ev.flags or {})
            hint = ("  ← the current period may be behind a tab/filter/page"
                    if q.stale and ev is not None
                    and (tabbed or ev.has_filters or ev.has_pagination or ev.interactive) else "")
            lines.append(f"  timeliness:{' ⚠️ STALE —' if q.stale else ' ✓'} {q.timeliness}{hint}")
        if q.completeness:  # does the query cover the WHOLE dataset? (pagination / filters)
            lines.append(f"  complete:  {'✓' if q.covers_all else '⚠️ subset —'} {q.completeness}")
        if q.correctness:  # is it the RIGHT set? (unfiltered, order known)
            lines.append(f"  correct:   {'✓' if q.correct else '⚠️'} {q.correctness}")
        if q.latest is not None:  # a distinct A (latest) companion for a paginated source
            lines.append(f"  queries:   A/latest — {q.latest.row_count} row(s), page one (a cheap "
                         f"incremental poll)  ·  B/all — {q.row_count} row(s), pagination walked")
        lines.append("  sample:")
        lines += _render_table(q.sample)
        if q.attempts:  # the rejection trail: why each earlier authoring attempt was rejected
            lines.append(f"  authoring: {len(q.attempts) + 1} attempt(s); earlier rejections:")
            for a in q.attempts:
                lines.append(f"    ✗ {a}")
        # the self-contained query blob(s) -- executable as is, easy to copy. A split query is
        # several section blobs the pipeline runs and CONCATENATES (via run_query); a plain
        # query is one blob runnable with `from_blob(blob).collect()`.
        if split:
            lines.append(f"  section blobs ({len(q.parts)}; run_query runs each + concatenates the rows):")
            for i, part in enumerate(q.parts):
                lines.append(f"    section {i + 1}: {part.describe}   ({part.row_count} row[s])")
                lines.append(f"    {part.blob}")
        else:
            lines.append("  query blob (copy; run with `from_blob(blob).collect()`):")
            lines.append(q.blob)
    if result.reviews:  # the integral stage reviews (gates) + a failure diagnosis
        lines.append("  review:")
        for r in result.reviews:
            mark = "" if r.stage == "failure" else ("✓ " if r.passed else "✗ ")
            grade = f"{r.verdict}" + (f", {r.score}/10" if r.score else "")
            head = f"    {mark}{r.stage}" + (f" ({grade})" if grade.strip(", ") else "")
            lines.append(head + (f": {r.summary}" if r.summary else ""))
            for issue in r.issues:
                lines.append(f"      · {issue}")
    lines.append(f"  spent:     ${result.cost_usd:.4f}")
    for line in lines:
        log.info(line)


