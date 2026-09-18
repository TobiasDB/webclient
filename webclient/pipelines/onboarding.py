"""Company onboarding: given a BRIEF (a dataset description) and a company, find the
most scrapeable source for that dataset and author a runnable lazy query for it.

The pipeline is seven small stages, each usable on its own; ``onboard_company``
wires them together:

1. :func:`search_web`        -- seed URLs for the company + brief.
2. :func:`crawl_from_seeds`  -- LLM-driven crawl: at each round the model picks the
                                frontier edges most likely to reach the dataset,
                                favouring a *queryable source* (an API over the whole
                                dataset) over a SPA that lists only part of it.
3. :func:`select_candidates` -- rank the crawled pages by scrapability + relevance
                                into must / should / could tiers.
4. :func:`evaluate_candidate`-- for a candidate, read its skeleton + flags and have
                                the model assess the dataset (present? sorted? complete?
                                paginated? filtered? a subset? unstructured? drill-down?).
5. :func:`write_reference`   -- the lazy ``Reference`` for the chosen candidate (its
                                data-API endpoint when the SPA has one, else its URL).
6. :func:`write_resolve`     -- the ``Resolve`` policy (deterministic given the page's
                                ``flags`` -- spa/anti_bot_triggered map to browser/proxy/stealth).

The chosen source then runs a flag-driven decision cascade (in ``onboard_company``):
reference -> the API endpoint if the SPA has one -> a login wall stops it -> the
resolve policy from spa/anti_bot_triggered -> the query, told to page when the
pagination flag is set. So the flags choose the URL, the resolve args, whether a
browser is needed, and whether to follow pagination.
7. :func:`write_query`       -- the lazy web query, authored by the model from the page
                                skeleton against the packaged query spec.

Everything the model does goes through one injected ``llm(prompt) -> str`` callable,
and web search through one injected ``search(query, k) -> list[SearchHit]`` callable,
so the whole pipeline runs offline against a stub model + a local server in tests.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Callable, Literal, Sequence

from pydantic import BaseModel, model_validator

#: the pipeline's logger. Stages log progress here (seeds, crawl, candidates, the
#: evaluation, the query, spend); a CLI or app sets the level / handler. Each line is
#: also appended to ``OnboardingResult.steps`` for a programmatic trace.
log = logging.getLogger("webclient.pipelines.onboarding")

from ..core.document.models import Flag
from ..core.reference.models import (
    AntiBotPolicy,
    BrowserPolicy,
    ProxyPolicy,
    Resolve,
)
from ..guides import lazy_query_guide
from ..query.expr import from_blob
from ..surfaces import Reference, WebClient, wq
from .llm import Budget, BudgetExceeded, LlmClient, LlmError
from .prompts import render_prompt

#: the model: a prompt in, its completion text out. Inject any client (a Claude call,
#: a local model, or a stub in tests). Kept deliberately minimal.
LLM = Callable[[str], str]
#: web search: a query + a result count in, ranked hits out.
SearchFn = Callable[[str, int], "list[SearchHit]"]
#: the transport tier for a fetch.
BrowserMode = Literal["never", "auto", "always"]


def _mode(browser: bool) -> BrowserMode:
    return "auto" if browser else "never"


#: the skeleton line budget for evaluation + query authoring -- generous enough to be
#: the WHOLE structure of essentially any real page (the model needs every record /
#: field), while staying well inside the model's context so an enormous page can't blow
#: it into a 400 "prompt too long".
_FULL_SKELETON = 4000

#: hard CHARACTER budgets for the big, page-derived prompt inputs (~4 chars/token), so
#: no single prompt can grow past the model's context and 400 as "prompt too long".
#: A clipped input keeps the most useful part for its content type (see :func:`_clip`)
#: and notes what was dropped.
_MAX_SKELETON_CHARS = 16_000   # ~4k tokens -- plenty to read a page's structure
_MAX_LISTING_CHARS = 6_000     # the frontier listing for pick_edges
_MAX_PAGES_CHARS = 10_000      # the crawled-pages JSON for select_candidates


def _skeleton_for(doc: Any) -> str:
    """The page skeleton to hand the model, clipped to budget. A big, repetitive page (a
    modern SPA whose records are hundreds of hashed-class siblings -- e.g. a pricing/blog grid
    that server-renders every item) blows past the budget as raw structure, so the record
    pattern is invisible in the clipped view. When the faithful skeleton is too large, re-render
    with ``collapse=True`` -- consecutive STRUCTURALLY-IDENTICAL siblings fold to one
    representative + ``×N`` -- so the repeating record and its fields are legible and the model
    can write a ``select_all`` for it. Small pages keep the faithful, every-sibling view."""
    kind = "json" if doc.kind == "json" else "html"
    skel = doc.skeleton(max_lines=_FULL_SKELETON)
    if len(skel) > _MAX_SKELETON_CHARS:
        # too big to read raw -> fold identical siblings AND drop nav/footer/sidebar chrome so
        # the record region isn't clipped away under menus (hashed classes are always dropped).
        skel = doc.skeleton(max_lines=_FULL_SKELETON, collapse=True, drop_chrome=True)
    return _clip(skel, _MAX_SKELETON_CHARS, "skeleton", kind=kind)


def _clip(text: str, max_chars: int, what: str = "input", *, kind: str = "head") -> str:
    """Keep ``text`` within ``max_chars`` so a huge page can't blow the prompt, trimming
    where the LEAST useful content is for that content type:

    - ``"html"``: keep the CENTRE (the records live in ``<main>``; nav/header/footer are
      chrome at the two ends), so trim evenly from both sides.
    - ``"json"``: keep both ENDS (the shape is at the head and the structure closes at
      the tail; the middle is repetitive array elements), so trim from the centre out.
    - ``"head"`` (default): keep the head (e.g. a best-first link listing).

    A trim leaves a note where content was dropped. Logs at debug when it trims."""
    if len(text) <= max_chars:
        return text
    over = len(text) - max_chars
    note = f"… [trimmed {over} chars of the {what}] …"
    log.debug("clipped %s: %d -> %d chars (%s)", what, len(text), max_chars, kind)
    if kind == "json":  # head + tail (drop the repetitive middle)
        half = max_chars // 2
        return text[:half] + "\n" + note + "\n" + text[-half:]
    if kind == "html":  # the centre (drop the chrome at both ends)
        cut = over // 2
        return note + "\n" + text[cut : cut + max_chars] + "\n" + note
    return text[:max_chars] + "\n" + note  # head


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
        lines); ``crawl`` (a mapping of pipeline crawl overrides -- ``max_pages`` / ``depth``
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

    @property
    def usable(self) -> bool:
        return self.dataset_present and self.scrapability >= 5


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
    reason: str = ""  # why, when not ok
    evaluation: CandidateEval | None = None
    reference: Any = None  # the lazy Reference (a core; not re-validated by pydantic)
    resolve: Resolve | None = None
    query: QueryArtifact | None = None
    steps: list[str] = []  # a human-readable trace of the run (also logged)
    reviews: list[Review] = []  # LLM meta-reviews grading the run's choices (opt-in)
    cost_usd: float = 0.0  # LLM spend for this company (when an LlmClient was used)


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
    lines.append(f"  result:    {'ready' if result.ok else 'not onboarded — ' + result.reason}")
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
        lines.append(f"  query:     {q.describe}")
        lines.append(f"  tested:    {'✓' if q.tested else '✗'}  {q.row_count} row(s)")
        if q.timeliness:  # the TIMELINESS flag: is the newest extracted row recent? (a flag, not a gate)
            hint = ("  ← the current period may be behind a tab/filter/page"
                    if q.stale and ev is not None
                    and (ev.has_filters or ev.has_pagination or ev.interactive) else "")
            lines.append(f"  timeliness:{' ⚠️ STALE —' if q.stale else ' ✓'} {q.timeliness}{hint}")
        lines.append("  sample:")
        lines += _render_table(q.sample)
        # the self-contained query blob on its own line -- executable as is, easy to copy
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


# --------------------------------------------------------------------------- #
# LLM plumbing: prompt -> parsed JSON, defensively.
# --------------------------------------------------------------------------- #


def _strip_fences(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[-1]  # drop the opening ``` / ```json line
        if t.rstrip().endswith("```"):
            t = t.rstrip()[:-3]
    return t.strip()


def _json_blob(text: str) -> str:
    """The first balanced JSON object/array in ``text`` (models like to wrap it in prose).
    STRING-AWARE: a brace/bracket inside a JSON string value (``"the } brace"``, or a selector
    like ``[class*="price"]`` in a reason) does not miscount depth. Falls back to the whole
    stripped string."""
    t = _strip_fences(text)
    starts = [i for i in (t.find("{"), t.find("[")) if i != -1]
    if not starts:
        return t
    start = min(starts)
    depth = 0
    in_str = False
    escaped = False
    for i in range(start, len(t)):
        ch = t[i]
        if in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            depth += 1
        elif ch in "}]":
            depth -= 1
            if depth == 0:
                return t[start : i + 1]
    return t[start:]


def _ask_json(llm: LLM, prompt: str, *, retries: int = 1) -> Any:
    """Run ``llm`` and parse a JSON value from its reply. On a decode error, retry --
    handing the model its own bad output + the parser error so it can fix it -- up to
    ``retries`` times. ``None`` if it still can't produce valid JSON."""
    ask = prompt
    for attempt in range(retries + 1):
        try:
            reply = llm(ask)
        except LlmError as exc:  # a bad-request / exhausted-retry API error -- don't crash
            log.warning("LLM call failed: %s -- skipping this step", exc)
            return None
        try:
            return json.loads(_json_blob(reply))
        except (json.JSONDecodeError, ValueError) as exc:
            if attempt == retries:
                log.warning("LLM reply was not valid JSON after %d tr[y|ies]", attempt + 1)
                return None
            ask = (
                f"{prompt}\n\n---\nYour previous reply could not be parsed as JSON: "
                f"{exc}. Here is what you sent:\n{reply[:800]}\n\nReply again with ONLY "
                "valid JSON -- no prose, no code fences."
            )
    return None


def _schema_outline(fields: "list[SchemaField]", indent: int = 0) -> str:
    """A :class:`SchemaField` tree rendered as an indented outline for a prompt --
    ``- name — description`` per field, nested children indented under their parent."""
    lines: list[str] = []
    for f in fields:
        opt = " (optional)" if f.optional else ""
        desc = f" — {f.description}" if f.description else ""
        lines.append("  " * indent + f"- {f.name}{opt}{desc}")
        if f.children:
            lines.append(_schema_outline(f.children, indent + 1))
    return "\n".join(line for line in lines if line)


def _fields_line(brief: Brief) -> str:
    """The brief's hints as an appended block for any prompt: the target schema (a
    nested outline with per-field descriptions, so the author knows the shape AND how
    to fill each field, nesting a sub-extract per branch) plus the natural-language
    ``look`` / ``ignore`` guides. Empty when the brief carries none."""
    parts: list[str] = []
    if brief.fields:
        parts.append(
            "Target schema (nest a sub-extract per branch so the output JSON matches):\n"
            + _schema_outline(brief.schema_tree())
        )
    if brief.look:
        parts.append("Head for pages like: " + "; ".join(brief.look) + ".")
    if brief.ignore:
        parts.append("Skip pages like: " + "; ".join(brief.ignore) + ".")
    return (" " + " ".join(parts)) if parts else ""


#: the flags the pipeline reads to decide how to fetch, resolve and query a source.
_DECISION_FLAGS = (
    "spa", "shadow_dom", "iframe", "anti_bot_triggered", "login_required",
    "pagination", "forms", "buttons", "large_document",
)


def _read_flags(doc: Any) -> "dict[str, Flag]":
    """The pipeline's decision flags for a document -- each read whether present or
    not, so a stage can branch on ``.present`` / ``.remedy`` / ``.value``."""
    return {name: getattr(doc, name)() for name in _DECISION_FLAGS}


# --------------------------------------------------------------------------- #
# 1. search_web
# --------------------------------------------------------------------------- #


def ddg_search(query: str, k: int = 6) -> "list[SearchHit]":
    """A default :data:`SearchFn` backed by the ``ddgs`` package (DuckDuckGo and
    friends). Optional dependency -- raises a clear error if it is not installed;
    inject your own ``search`` callable to avoid it."""
    try:
        from ddgs import DDGS  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover - exercised only without the dep
        raise RuntimeError(
            "web search needs the 'ddgs' package (pip install ddgs), or pass your "
            "own search=... callable to search_web()/onboard_company()."
        ) from exc
    # `backend` names the search engine(s) ddgs queries: "auto" lets it fall through its list
    # (duckduckgo, google, bing, brave, ...). Override with the WEBCLIENT_SEARCH_BACKEND env var.
    backend = os.environ.get("WEBCLIENT_SEARCH_BACKEND", "auto")
    try:
        with DDGS() as ddgs:
            rows = ddgs.text(query, max_results=k, backend=backend) or []
    except Exception as exc:  # noqa: BLE001 - ddgs raises on rate limits / no results / timeouts;
        # a search backend hiccup is not fatal -- return no seeds so the caller can retry with a
        # different term (see search_web) instead of crashing the whole onboarding run.
        log.info("    web search error for %r (backend=%s): %s -- treating as no results",
                 query, backend, exc)
        return []
    log.info("    web search via ddgs (backend=%s): %d result(s) for %r", backend, len(rows), query)
    return [
        SearchHit(
            url=str(r.get("href") or r.get("url") or ""),
            title=str(r.get("title") or ""),
            snippet=str(r.get("body") or r.get("snippet") or ""),
        )
        for r in rows
        if r.get("href") or r.get("url")
    ]


def _apply_search_term(company: str, term: str) -> str:
    """``"<company> <term>"``, or ``term`` with a literal ``{company}`` placeholder filled in.
    Empty ``term`` -> the bare company name."""
    term = term.strip()
    if not term:
        return company.strip()
    if "{company}" in term:
        return term.replace("{company}", company).strip()
    return f"{company} {term}".strip()


def _search_query(brief: Brief, company: str, *, mode: str = "normal") -> str:
    """The web-search query for ``company`` -- DETERMINISTIC, from the brief's ``search``
    qualifier: ``"<company> <brief.search>"`` (e.g. ``"Adobe investor relations news"``).
    ``mode`` steers a retry: ``"broaden"`` drops the qualifier for the bare company name (plus a
    site-type hint); ``"disambiguate"`` adds a distinguishing hint when a look-alike came back."""
    if mode == "broaden":  # got nothing -- simplest possible: name + one site-type hint
        hint = (brief.look[0] if brief.look else "") or ""
        return f"{company} {hint}".strip() or company.strip()
    query = _apply_search_term(company, brief.search)
    if mode == "disambiguate":  # a look-alike came back -- add a distinguishing hint
        hint = (brief.look[0] if brief.look else "") or brief.title
        query = f"{query} {hint}".strip()
    return query or company.strip()


def _seeds_for_company(seeds: "list[Seed]", company: str, brief: Brief, llm: "LLM | None") -> "list[Seed]":
    """Keep only the seeds that actually belong to ``company`` -- the model rejects
    look-alike companies with a similar name (``Square`` when we asked for ``Squarepoint``),
    unrelated orgs and aggregators. Fails OPEN: if the model gives no usable judgement, all
    seeds are kept (never silently drop everything on a bad reply)."""
    if llm is None or not seeds:
        return list(seeds)
    listing = "\n".join(f"{i}. {s.url}  [{s.title}]  {s.why}"[:300] for i, s in enumerate(seeds))
    data = _ask_json(llm, render_prompt(
        "verify_seeds", company=company, description=brief.description,
        fields_line=_fields_line(brief),  # the brief guides every decision, this one included
        seeds=_clip(listing, _MAX_LISTING_CHARS, "seed results"),
    ))
    belong = data.get("belong") if isinstance(data, dict) else None
    if not isinstance(belong, list):
        return list(seeds)  # fail open -- no usable judgement
    keep = {i for i in belong if isinstance(i, int)}
    kept = [s for i, s in enumerate(seeds) if i in keep]
    for i, s in enumerate(seeds):
        if i not in keep:
            log.info("    dropped off-company seed: %s [%s]", s.url, s.title)
    return kept


def search_web(
    brief: Brief,
    company: str,
    *,
    search: SearchFn,
    k: int = 6,
    llm: LLM | None = None,
) -> list[Seed]:
    """Seed URLs for ``company`` + ``brief``. The query is DETERMINISTIC -- ``"<company>
    <brief.search>"`` (the brief's ``search`` qualifier). Pass ``llm`` to verify each result
    really belongs to ``company`` (dropping look-alike companies with a similar name). If the
    whole first result set is the wrong company, the search retries with a disambiguating hint;
    no results -> it broadens to the bare company name. FAIL-OPEN: if verification would drop
    EVERY result on both tries, the raw results are returned anyway rather than sinking the
    company on a stubborn/erroneous LLM judgement."""
    raw: list[Seed] = []
    mode = "normal"
    tried: set[str] = set()
    for _attempt in range(3):
        query = _search_query(brief, company, mode=mode)
        if query in tried:  # don't re-issue the same query -- force a simpler, different one
            query = f"{company} {(brief.look[0] if brief.look else brief.name or brief.title)}".strip() or company
        tried.add(query)
        log.info("    search query: %r", query)
        seeds = [Seed(url=h.url, title=h.title, why=h.snippet) for h in search(query, k) if h.url]
        if not seeds:  # NO results (empty, or a backend error) -- BROADEN and retry a different term
            log.info("    no results for %r -- retrying the search, broader", query)
            mode = "broaden"
            continue
        raw = raw or seeds  # remember the first non-empty result set for the fail-open path
        kept = _seeds_for_company(seeds, company, brief, llm)
        if kept:
            if len(kept) < len(seeds):
                log.info("    %d/%d result(s) belong to %s", len(kept), len(seeds), company)
            return kept
        # results came back but none were this company -- try a stricter, disambiguating query
        log.info("    no result belongs to %s -- retrying the search, stricter", company)
        mode = "disambiguate"
    if raw:  # verification killed everything -- crawl the raw seeds rather than give up
        log.info("    verification dropped all results for %s -- using the raw seeds", company)
    return raw


# --------------------------------------------------------------------------- #
# 2. crawl_from_seeds  (LLM-driven frontier)
# --------------------------------------------------------------------------- #


def _frontier_key(url: str) -> "tuple[str, str, frozenset[str]]":
    """A dedup key that collapses a paginated set and repeated calls to one API: the
    host + the path with any ``/page/N`` segment stripped + the set of query-param
    KEYS (ignoring their values). So ``?page=1`` / ``?page=2`` and ``/list/page/3``
    collapse to one, while distinct resources (``/item/1`` vs ``/item/2``) stay apart."""
    import re
    from urllib.parse import parse_qsl, urlsplit

    parts = urlsplit(url)
    path = re.sub(r"/(?:page|p)/\d+", "", parts.path).rstrip("/") or "/"
    # ignore pagination params so ?page=1 / ?page=2 / /page/3 all collapse together
    keys = frozenset(
        k for k, _ in parse_qsl(parts.query) if k.lower() not in _PAGE_PARAMS
    )
    return (parts.netloc, path, keys)


#: query params that only page/window a result set (not a distinct resource).
_PAGE_PARAMS = {
    "page", "p", "pg", "pagenum", "offset", "start", "limit", "per_page", "cursor",
}

#: path fragments that mark a page as API / product DOCUMENTATION rather than data. A
#: docs page is never a scrapable dataset, so it is HARD-BANNED from the crawl (never
#: expanded, never a candidate) -- kept specific so a data endpoint like ``/api/v1/
#: products`` is NOT caught (only ``/api-docs`` etc.).
_DOCS_HINTS = (
    "/docs", "/doc/", "/documentation", "/developer", "/dev-docs", "/api-docs",
    "/apidocs", "/api-reference", "/reference/", "/swagger", "/openapi", "/redoc",
    "/guide", "/tutorial", "/sdk", "/faq", "/help/", "/knowledge", "/manual",
)


def _is_docs_url(url: str) -> bool:
    """Whether ``url`` is API / product DOCUMENTATION (a ``docs.`` / ``developer.``
    host, or a docs path fragment) -- hard-banned from the crawl."""
    from urllib.parse import urlsplit

    parts = urlsplit(url.lower())
    host = parts.hostname or ""
    if host.startswith(("docs.", "developer.", "developers.", "apidocs.")):
        return True
    path = parts.path.rstrip("/") + "/"  # so a trailing "/docs" matches "/docs/"
    return any(hint in path for hint in _DOCS_HINTS)


#: URL fragments that mark a feed / data endpoint (a page that IS the dataset, not one
#: that links to it) -- used alongside the sniffed ``kind`` so a served-as-text feed counts.
_DATA_DOC_HINTS = (".rss", ".atom", "/rss", "/feed", "/atom", ".json", "/api/", "/api.")


def _is_data_doc(card: Any) -> bool:
    """Whether a crawled page IS a data document -- a JSON/XML body (sniffed ``kind``), or a
    feed/API URL. Such a page holds the dataset directly, so it should be a candidate outright
    rather than left to the LLM filter (which judges only url+title and tends to drop it)."""
    if getattr(card, "kind", "html") in ("json", "xml"):
        return True
    u = (getattr(card, "final_url", None) or card.url).lower()
    return any(h in u for h in _DATA_DOC_HINTS)


def _reg_domain(url: str) -> str:
    """The registrable domain (eTLD+1) of ``url`` -- the identity we bind the crawl to,
    so ``news.adobe.com`` / ``www.adobe.com`` / ``milo.adobe.com`` all read as ``adobe.com``."""
    from urllib.parse import urlsplit

    from ..core.crawl.canon import _registrable

    return _registrable((urlsplit(url).hostname or "").lower())


def _seed_domains(seeds: Sequence[Seed]) -> "set[str]":
    """The registrable domains the search surfaced for THIS company -- the company's web
    footprint. The crawl is kept inside it so it can't wander onto a different company's
    site discovered mid-crawl (the search query was ``"<company> <brief>"``, so the seed
    domains are the company's own)."""
    return {d for s in seeds if s.url and (d := _reg_domain(s.url))}


def _filter_frontier(
    edges: Sequence[Any], brief: Brief, *, allow_domains: "frozenset[str] | set[str]" = frozenset()
) -> list[Any]:
    """Prune the frontier before the model spends a pick on it: keep only the company's
    own domains (``allow_domains`` -- so it can't drift onto an unrelated company), HARD-BAN
    documentation pages (:func:`_is_docs_url` -- never a dataset), then collapse paginated
    URL sets and repeated similar-API calls to one representative each (keeping the first --
    the frontier is already best-first). ``look`` / ``ignore`` stay natural-language guides
    the model applies; this filter is purely structural."""
    kept: list[Any] = []
    seen: set[tuple[str, str, frozenset[str]]] = set()
    for e in edges:
        if allow_domains and _reg_domain(e.url) not in allow_domains:
            log.debug("off-company URL dropped from the frontier: %s", e.url)
            continue
        if _is_docs_url(e.url):
            log.debug("banned docs URL from the frontier: %s", e.url)
            continue
        key = _frontier_key(e.url)
        if key in seen:
            continue
        seen.add(key)
        kept.append(e)
    return kept


def _pick_edges(llm: LLM, brief: Brief, frontier: Sequence[Any], *, company: str = "") -> list[str]:
    """Ask the model which frontier edges to expand next -- the ones most likely to
    reach the dataset, preferring a queryable source (an API over the whole dataset)
    to a page that lists only part of it, and following pagination when it must.
    ``company`` is named so the model stays on that company's pages and skips any that
    belong to a different organisation."""
    listing = _clip(
        "\n".join(f"{i}. {e.url}   (link text: {e.text!r})" for i, e in enumerate(frontier)),
        _MAX_LISTING_CHARS, "frontier listing",
    )
    picked = _ask_json(
        llm,
        render_prompt(
            "pick_edges",
            company=company or "the company",
            description=brief.description,
            fields_line=_fields_line(brief),
            listing=listing,
        ),
    )
    if not isinstance(picked, list):
        return []
    urls: list[str] = []
    for item in picked:
        # accept {"n": i, "why": "..."} (reasoned) or a bare index for robustness
        if isinstance(item, dict):
            idx, why = item.get("n", item.get("index")), str(item.get("why") or item.get("reason") or "")
        else:
            idx, why = item, ""
        if isinstance(idx, int) and 0 <= idx < len(frontier):
            urls.append(frontier[idx].url)
            log.info("    pick %s%s", frontier[idx].url, f"  — {why}" if why else "")
    return urls


def crawl_from_seeds(
    seeds: Sequence[Seed],
    brief: Brief,
    *,
    wc: WebClient,
    llm: LLM,
    company: str = "",
    max_pages: int = 20,
    rounds: int = 4,
    browser: bool = True,
) -> Any:
    """A hand-driven crawl steered by the model: fetch the seeds, then each round let
    the model pick which discovered edges to expand (favouring a queryable source),
    up to ``rounds`` rounds or ``max_pages`` pages. Returns the finished ``Crawl``
    (read ``.pages`` for the retained Documents). The brief's ``crawl`` block overrides
    ``max_pages`` / ``rounds`` / ``depth`` / ``browser`` per dataset. The crawl is bound
    to the company's own domains (the seed footprint) so it can't wander onto a different
    company's site."""
    cfg = brief.crawl
    max_pages = int(cfg.get("max_pages", max_pages))
    rounds = int(cfg.get("rounds", rounds))
    browser = cfg.get("browser", browser)  # bool or a tier ("auto"/"always"/"never")
    depth = int(cfg.get("depth", 3))
    seed_urls = [s.url for s in seeds if s.url]
    domains = _seed_domains(seeds)  # the company's web footprint -- the crawl stays inside it
    # The seed URLs the search surfaced, printed BEFORE the model filters/picks them.
    log.info("    %d seed URL(s) for %s (domains: %s):",
             len(seed_urls), company or "the company", ", ".join(sorted(domains)) or "?")
    for s in seeds:
        if s.url:
            log.info("      seed %s%s", s.url, f"  — {s.title}" if s.title else "")
    crawl = wc.crawl(
        seed_urls, auto=False, browser=browser, max_pages=max_pages, depth=depth,
        obey_robots=False, allow_domains=sorted(domains),
    )
    seen_pages = seen_fails = 0
    # The seeds sit in the frontier UNFETCHED: round 0 lets the model evaluate the seeds
    # and pick which to fetch (not blindly fetch them all), and each further round picks
    # from newly-discovered edges -- so a docs/irrelevant seed is dropped before it costs
    # a fetch. If the model rejects every seed on round 0, fall back to the filtered seeds
    # so the crawl still gets off the ground.
    for round_i in range(rounds + 1):
        if not crawl.frontier or len(crawl.pages) >= max_pages:
            break
        # keep to the company's domains, collapse paginated/similar-API dups, ban docs pages
        candidates = _filter_frontier(list(crawl.frontier), brief, allow_domains=domains)
        if not candidates:
            break
        picks = _pick_edges(llm, brief, candidates, company=company)
        if not picks and round_i == 0:
            log.info("    model chose no frontier links this round — fetching the seed(s) directly")
            picks = [e.url for e in candidates]
        if not picks:
            break
        crawl.step(picks)
        seen_pages, seen_fails = _log_crawl_progress(crawl, seen_pages, seen_fails)
    return crawl


def _log_crawl_progress(crawl: Any, seen_pages: int, seen_fails: int) -> "tuple[int, int]":
    """Log each newly-fetched URL since the last round with the TRANSPORT it used (http vs
    browser -- from the page's final tier) and the SIGNALS that fired on it (the flag
    names), plus each failed edge + why. So the crawl is observable page by page: you can
    see when a page forced a browser escalation and what it tripped."""
    for card in crawl.pages[seen_pages:]:
        # the FULL resolve trail, so it's explicit whether http was enough or a browser (and
        # any proxy) was needed: "http" (static only) / "http→browser" / "http→proxy→browser".
        esc = getattr(card, "escalation", None) or [getattr(card, "final_tier", "static") or "static"]
        transport = "→".join("http" if t == "static" else t for t in esc)
        flags = ", ".join(getattr(card, "flags", []) or []) or "none"
        log.info("    crawl [%s] %s  (via %s; signals: %s)",
                 card.status_code, card.final_url or card.url, transport, flags)
    for fail in crawl.failures[seen_fails:]:
        log.info("    crawl [%s] %s  (%s)", fail.status_code or "x", fail.url, fail.reason)
    return len(crawl.pages), len(crawl.failures)


# --------------------------------------------------------------------------- #
# 3. select_candidates
# --------------------------------------------------------------------------- #


def select_candidates(
    crawl: Any, brief: Brief, *, llm: LLM, seed_urls: "Sequence[str]" = ()
) -> list[Candidate]:
    """Rank the crawled pages into must / should / could-evaluate candidates by
    scrapability + likely relevance to the dataset. A SEED that is itself a data document
    (a feed/JSON the caller pointed us at) is forced in as a candidate -- but only a seed,
    never every feed a site happens to expose."""
    # crawl.pages are lean PageCards by default (url / title / kind / flags projected).
    # hard-ban documentation pages: even if one was fetched (a seed / a stray pick), it
    # is never a scrapable dataset, so it can't become a candidate.
    usable = [p for p in crawl.pages if not _is_docs_url(p.final_url or p.url)]
    pages = [
        {"url": p.final_url or p.url, "title": p.title, "kind": p.kind, "flags": p.flags}
        for p in usable
    ]
    if not pages:
        return []
    rows = _ask_json(
        llm,
        render_prompt(
            "select_candidates",
            description=brief.description,
            fields_line=_fields_line(brief),
            pages_json=_clip(json.dumps(pages, indent=0), _MAX_PAGES_CHARS, "pages list", kind="json"),
        ),
    )
    out: list[Candidate] = []
    for r in rows if isinstance(rows, list) else []:
        if isinstance(r, dict) and r.get("url"):
            note = str(r.get("reason") or r.get("note") or "")  # the model's WHY
            out.append(Candidate.model_validate({**r, "url": str(r["url"]), "note": note}))
    picked = {c.url for c in out}
    # A SEED that is itself a DATA DOCUMENT (a JSON/XML feed or an API response the caller
    # pointed us at) IS the dataset -- it holds the records, it doesn't "lead to" them. The LLM
    # filter judges only url+title and routinely drops a raw feed/JSON, so force the SEED in as a
    # MUST candidate. Restricted to seeds ON PURPOSE: a site can expose many feeds (per-category,
    # comments, ...) and force-including every discovered feed would flood the candidates.
    seeds = set(seed_urls)
    for p in usable:
        u = p.final_url or p.url
        if u not in picked and _is_data_doc(p) and (u in seeds or p.url in seeds):
            out.append(Candidate(url=u, tier="must",
                                 note="a seeded data document (feed / JSON / API) — the dataset itself"))
            picked.add(u)
    # FAIL OPEN: the filter is an LLM and can return nothing on pages it should have kept
    # (variance, or a parse miss on the cheapest model). If it picked nothing yet we DID crawl
    # usable pages, keep the top few so the run still evaluates a real source rather than dying
    # at "no candidate pages" (evaluate_candidate then judges whether the dataset is actually there).
    if not out and usable:
        for p in usable[:3]:
            out.append(Candidate(url=(p.final_url or p.url), tier="could",
                                 note="fail-open: candidate filter returned nothing — kept for evaluation"))
    _rank = {"must": 0, "should": 1, "could": 2}
    out.sort(key=lambda c: _rank.get(c.tier, 3))
    for c in out:
        log.info("    candidate [%s] %s%s", c.tier, c.url, f"  — {c.note}" if c.note else "")
    return out


# --------------------------------------------------------------------------- #
# 4. evaluate_candidate
# --------------------------------------------------------------------------- #


def evaluate_candidate(
    candidate: Candidate,
    brief: Brief,
    *,
    wc: WebClient,
    llm: LLM,
    browser: BrowserMode = "auto",
) -> CandidateEval:
    """Fetch the candidate, read its skeleton + signals, and have the model judge the
    dataset (present, sorted, complete, paginated, filtered, a subset, unstructured,
    drill-down)."""
    doc = wc.fetch(candidate.url, browser=browser, optional=True)
    if not doc.ok:
        return CandidateEval(url=candidate.url, verdict="fetch failed")
    flags = _read_flags(doc)  # the detected conclusions (spa / pagination / login / ...)
    flag_map = {n: round(f.confidence, 2) for n, f in flags.items() if f.present}
    # the evidence behind each present flag -- the signals that fired, for the summary
    flag_signals = {
        n: [f"{s.name} ({s.stage}, {s.confidence:.2f}): {s.reason}" for s in f.signals]
        for n, f in flags.items() if f.present
    }
    # a login wall blocks the dataset -- no query reaches it; drop the candidate early.
    if flags["login_required"].present:
        return CandidateEval(url=candidate.url, verdict="login required",
                             flags=flag_map, flag_signals=flag_signals)
    skeleton = _skeleton_for(doc)
    endpoints = [c.url for c in doc.xhr_endpoints()]
    interactive = flags["forms"].present or flags["buttons"].present
    parsed = _ask_json(
        llm,
        render_prompt(
            "evaluate_candidate",
            description=brief.description,
            fields_line=_fields_line(brief),
            candidate_url=candidate.url,
            flag_map_json=json.dumps(flag_map),
            endpoints_json=json.dumps(endpoints),
            skeleton=skeleton,
        ),
    )
    data: dict[str, Any] = dict(parsed) if isinstance(parsed, dict) else {"verdict": "could not evaluate"}
    # the flags are ground truth for structure -> they win over the model's guesses.
    data["has_pagination"] = bool(data.get("has_pagination")) or flags["pagination"].present
    # The API endpoint is used ONLY if the model names one of the OBSERVED same-origin
    # XHR endpoints (never a blind "first XHR" pick, and never a hallucinated URL) -- so
    # the reference stays the CHOSEN page unless a real data endpoint is identified. This
    # fixes the reference pointing at a different URL than the source.
    llm_ep = data.get("api_endpoint")
    api_endpoint = llm_ep if (isinstance(llm_ep, str) and llm_ep in set(endpoints)) else None
    # an API-documentation page is never the data source -- guard even if the model was
    # inconsistent (this is the "docs page mistaken for the API" fix).
    if data.get("is_api_docs"):
        data["dataset_present"] = False
        data["is_queryable"] = False
        api_endpoint = None
    data.update(url=candidate.url, flags=flag_map, flag_signals=flag_signals,
                api_endpoint=api_endpoint, interactive=interactive)
    ev = CandidateEval.model_validate(data)
    log.info(
        "    evaluated %s -> present=%s, queryable=%s, scrapability=%d%s — %s",
        candidate.url, ev.dataset_present, ev.is_queryable, ev.scrapability,
        " [API DOCS]" if ev.is_api_docs else "", ev.verdict or "(no reason given)",
    )
    return ev


def evaluate_candidates(
    candidates: Sequence[Candidate],
    brief: Brief,
    *,
    wc: WebClient,
    llm: LLM,
    browser: BrowserMode = "auto",
) -> CandidateEval | None:
    """Evaluate candidates best-tier-first until a usable source is found (returns
    it) or the options run out (returns the best-scoring evaluation seen, or None).
    Prefers a queryable source, but by a WEIGHTED score -- a much cleaner scrapeable page
    can still beat a marginally-queryable messy one (see :func:`_candidate_score`)."""
    best: CandidateEval | None = None
    for c in candidates:
        ev = evaluate_candidate(c, brief, wc=wc, llm=llm, browser=browser)
        if best is None or _candidate_score(ev) > _candidate_score(best):
            best = ev
        if ev.usable and ev.is_queryable:
            return ev  # a queryable source clean enough to scrape -- stop early
    return best


def _candidate_score(ev: CandidateEval) -> float:
    """Rank a candidate: scrapability (0-10) plus a queryable BONUS -- so a queryable source
    is preferred, but not absolutely (a scrapability-10 page beats a scrapability-1 API). A
    source without the dataset is never preferred over one that has it."""
    if not ev.dataset_present:
        return -1.0
    return ev.scrapability + (4 if ev.is_queryable else 0)


# --------------------------------------------------------------------------- #
# 5. write_reference  (deterministic given the candidate)
# --------------------------------------------------------------------------- #


def _source_url(evaluation: CandidateEval) -> str:
    """The URL to root the reference + query at. Prefer an observed data API (``api_endpoint``)
    over the page ONLY when the page is a client-rendered SHELL (the ``spa`` flag fired) whose
    records come from that API. A directly-scrapable page -- data already in the served/rendered
    HTML -- is queried as the PAGE ITSELF even if it also fired an XHR (a secondary fetch,
    analytics, or a model mis-pick), so we never reference an XHR when the page was the right
    source (the query was authored + tested against this URL)."""
    if evaluation.api_endpoint and evaluation.flags.get("spa"):
        return evaluation.api_endpoint
    return evaluation.url


def write_reference(evaluation: CandidateEval, *, wc: WebClient) -> Reference:
    """The lazy ``Reference`` for the chosen source -- deterministic given the candidate. Rooted
    at the page URL, or at a same-origin data API only when the page is an SPA shell backed by it
    (see :func:`_source_url`)."""
    return wc.ref(_source_url(evaluation))


# --------------------------------------------------------------------------- #
# 6. write_resolve  (deterministic given the flags)
# --------------------------------------------------------------------------- #


def write_resolve(flags: Sequence[Flag]) -> Resolve:
    """The ``Resolve`` policy for the source -- deterministic from its flags. A
    ``spa`` needs a browser render; an ``anti_bot_triggered`` needs its remedy
    (``proxy`` for a bare block, ``stealth`` = a browser behind a proxy with anti-bot
    handling for a named vendor). A login wall has no transport remedy."""
    by = {f.name: f for f in flags if f.present}
    # a browser is needed to build the DOM: an SPA composes it client-side; shadow DOM /
    # a same-origin iframe hides content a plain HTML snapshot misses, and only a render
    # inlines it (see the __wc_inline page script).
    needs_render = any(n in by for n in ("spa", "shadow_dom", "iframe"))
    triggered = by.get("anti_bot_triggered")
    stealth = bool(triggered and triggered.remedy == "stealth")
    proxy = bool(triggered and triggered.remedy in ("proxy", "stealth"))
    return Resolve(
        browser=BrowserPolicy(when="always") if (needs_render or stealth) else None,
        proxy=ProxyPolicy.auto() if proxy else None,
        antibot=AntiBotPolicy.auto() if stealth else None,
    )


# --------------------------------------------------------------------------- #
# 7. write_query  (LLM authors a lazy query from the skeleton)
# --------------------------------------------------------------------------- #


def _query_prompt(brief: Brief, skeleton: str, *, paginated: bool = False, recency: str = "") -> str:
    # Deliberately narrow: the packaged query spec + the skeleton + the ask. Nothing
    # about fetching, resolving, or running -- only CSS selectors and the query syntax.
    pager = (
        "\nThe dataset spans multiple pages: also extract the next-page link "
        '(a rel="next" anchor) as a field named "next" so the caller can follow it.'
        if paginated else ""
    )
    return render_prompt(
        "write_query",
        guide=lazy_query_guide(),
        description=brief.description,
        fields_line=_fields_line(brief),
        pager=pager,
        skeleton=skeleton,
        recency=(f"\n\nRECENCY (from the page evaluation): {recency}" if recency else ""),
    )


def _recency_guidance(ev: "CandidateEval | None") -> str:
    """A short recency instruction for the query writer, from the evaluator's read of the
    sort order + where the most recent records are. Empty for a non-dated dataset."""
    if ev is None:
        return ""
    parts = []
    if ev.sort_order:
        parts.append(f"the records are {ev.sort_order}")
    if ev.recency_hint:
        parts.append(ev.recency_hint)
    if not parts:
        return ""
    return "Prioritise the MOST RECENT records -- " + "; ".join(parts) + "."


def _data_rows(result: Any) -> list[Any]:
    """The EXTRACTED DATA a query produced -- plain rows (dicts / scalars), never the
    elements themselves. A query that stops at ``select_all`` yields a collection of
    ``Document`` elements: that is a selection, NOT extracted data, so it counts as
    zero rows (the author must ``.project()``). This is what stops an un-projected
    query from looking like a success and what keeps the sample as data, not objects."""
    from ..core.web_core import WebCore

    if result is None or isinstance(result, WebCore):
        return []  # None, or a single selected element -- not data
    try:
        items = list(result)
    except TypeError:  # a scalar / Field -- one value
        return [result]
    if items and all(isinstance(i, WebCore) for i in items):
        return []  # a collection of elements -- selected, not extracted (no project)
    return items


def _extraction_steps(doc_expr: Any) -> list[Any]:
    """The model's EXTRACTION steps only -- from the first ``select``/``select_all``
    onward -- dropping any navigation (a stray ``resolve``) it may have prefixed. So the
    join with the reference + resolve is DETERMINISTIC: the model supplies the selection,
    the pipeline supplies exactly one reference + one resolve."""
    steps = list(doc_expr._plan.steps)
    for i, s in enumerate(steps):
        if s.kind == "get" and s.name in ("select", "select_all"):
            return steps[i:]
    return steps


def _executable_query(doc_expr: Any, url: str, resolve: "Resolve | None") -> Any:
    """DETERMINISTICALLY wrap the model's DOCUMENT-level extraction into a SELF-CONTAINED
    query rooted at the source reference with a ``resolve`` step baked in, so
    ``from_blob(blob).collect()`` fetches + resolves + extracts with no context --
    executable exactly as output. The model supplies only the extraction; this function
    (no LLM) supplies the reference + resolve. When the source needs proxy / antibot, the
    FULL policy is baked in (``resolve(policy=...)``) so the blob re-fetches with it; a
    plain source just bakes the browser tier."""
    from ..query.expr import Expr
    from ..query.plan import Plan

    ref = wq.reference(url)
    if resolve is not None and (resolve.proxy is not None or resolve.antibot is not None):
        rooted = ref.resolve(policy=resolve.model_dump(mode="json"))  # full policy in the blob
    else:
        tier = resolve.browser.when if (resolve is not None and resolve.browser is not None) else None
        rooted = ref.resolve(browser=tier) if tier else ref.resolve()
    steps = [*rooted._plan.steps, *_extraction_steps(doc_expr)]
    return Expr(Plan(root="Reference", source=rooted._plan.source, steps=steps), doc_expr._client)


def _reroot(expr: Any, url: str) -> Any:
    """A copy of a reference-rooted executable query re-pointed at ``url`` (so ONE
    authored query runs against each of several base URLs)."""
    from ..core.reference import from_url
    from ..query.expr import Expr
    from ..query.plan import Plan

    return Expr(
        Plan(root="Reference", source=from_url(url).model_dump(), steps=expr._plan.steps),
        expr._client,
    )


def _query_code(reply: str) -> str:
    """The query EXPRESSION from the model's reply: strip any code fence / prose and start
    at the first ``wq.`` so a leading ``query =`` assignment or preamble is dropped, and cut
    a trailing code fence (a model that wraps the code in ``` despite the ask)."""
    t = _strip_fences(reply)
    i = t.find("wq.")
    if i != -1:
        t = t[i:]
    fence = t.find("```")  # a trailing fence when prose preceded the opening one
    if fence != -1:
        t = t[:fence]
    return t.strip()


#: constant literals a query may contain (selectors, group indices, flags).
_QUERY_CONST = (str, int, float, bool, bytes, type(None))


def _eval_query_ast(node: Any, root: Any) -> Any:
    """Interpret ONE node of a written query, driving the REAL ``wq`` interface -- attribute
    access and method calls on our own Expr / backing ops only. This is NOT ``eval``: the
    only name is ``wq``, attributes starting with ``_`` are refused (so ``__globals__`` /
    ``__class__`` and the builtins they reach are unreachable), only literal constants and
    the query operators (``& | ~`` and the comparisons used in ``filter``) are allowed, and
    anything else raises. So a prompt-injected line like
    ``wq.reference.__globals__['os'].system(...)`` cannot execute -- it is rejected at the
    ``__globals__`` attribute, never run."""
    import ast

    if isinstance(node, ast.Expression):
        return _eval_query_ast(node.body, root)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, _QUERY_CONST):
            return node.value
        raise ValueError(f"disallowed constant: {node.value!r}")
    if isinstance(node, ast.Name):
        if node.id == "wq":
            return root
        raise ValueError(f"only 'wq' is available in a query, not {node.id!r}")
    if isinstance(node, ast.Attribute):
        if node.attr.startswith("_"):
            raise ValueError(f"attribute {node.attr!r} is not allowed in a query")
        return getattr(_eval_query_ast(node.value, root), node.attr)
    if isinstance(node, ast.Call):
        func = _eval_query_ast(node.func, root)
        if any(isinstance(a, ast.Starred) for a in node.args):
            raise ValueError("*args are not allowed in a query")
        args = [_eval_query_ast(a, root) for a in node.args]
        kwargs: dict[str, Any] = {}
        for kw in node.keywords:
            if kw.arg is None:
                raise ValueError("**kwargs are not allowed in a query")
            kwargs[kw.arg] = _eval_query_ast(kw.value, root)
        return func(*args, **kwargs)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Invert):  # ~cond in a filter
        return ~_eval_query_ast(node.operand, root)
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.BitAnd, ast.BitOr)):  # a & b / a | b
        left, right = _eval_query_ast(node.left, root), _eval_query_ast(node.right, root)
        return (left & right) if isinstance(node.op, ast.BitAnd) else (left | right)
    if isinstance(node, ast.Compare) and len(node.ops) == 1:  # a == b, a < b, ...
        import operator as _op
        ops = {ast.Eq: _op.eq, ast.NotEq: _op.ne, ast.Lt: _op.lt,
               ast.LtE: _op.le, ast.Gt: _op.gt, ast.GtE: _op.ge}
        fn = ops.get(type(node.ops[0]))
        if fn is None:
            raise ValueError("that comparison is not allowed in a query")
        return fn(_eval_query_ast(node.left, root), _eval_query_ast(node.comparators[0], root))
    raise ValueError(f"disallowed expression in a query: {type(node).__name__}")


def _parse_query(reply: str) -> Any:
    """Load the model's query. The model WRITES it as a ``wq.doc`` chain -- exactly as the
    guide documents -- and we rebuild it THROUGH OUR OWN INTERFACE: the code is parsed to an
    AST and interpreted by :func:`_eval_query_ast`, which drives only the real ``wq`` Expr /
    backing ops (attribute access + method calls with literal args, plus the query operators).
    It is NOT ``eval`` -- a prompt-injected line reaching ``__globals__`` or any non-``wq``
    name is refused before anything runs, so a hostile crawled page cannot achieve code
    execution. A raw ``to_blob()`` blob is still accepted as a fallback."""
    from ..query.expr import Expr

    import ast

    code = _query_code(reply)
    if code.startswith("wq."):
        # rebuild THROUGH OUR INTERFACE via a controlled AST walk -- NOT eval(): a
        # prompt-injected line reaching __globals__ or a non-wq name is refused first.
        expr = _eval_query_ast(ast.parse(code, mode="eval"), wq)
        if not isinstance(expr, Expr):
            raise TypeError(f"query is a {type(expr).__name__}, not a wq.doc chain")
        return _normalize_selectors(expr)  # CSS '>' child combinator -> safer descendant space
    return _normalize_selectors(from_blob(_json_blob(reply)))  # fallback: a raw blob


def _row_selector(expr: Any) -> "str | None":
    """The CSS/selector string the query selects its repeating record with -- the argument
    of the first ``select``/``select_all``. Used to diagnose a 0-row query: if this
    selector matches nothing on the page, the row selector itself is wrong."""
    steps = list(expr._plan.steps)
    for i, s in enumerate(steps):
        if s.kind == "get" and s.name in ("select", "select_all"):
            nxt = steps[i + 1] if i + 1 < len(steps) else None
            if nxt is not None and nxt.kind == "call" and nxt.args:
                return getattr(nxt.args[0], "value", None)
    return None


def _selector_match_count(sel: "str | None", doc: Any) -> "int | None":
    """How many elements ``sel`` matches on the fetched ``doc`` (``None`` if it can't be
    probed). Lets the feedback tell the model whether its ROW selector is wrong (0 matches)
    or whether the record matches but the FIELD extraction is (matches, but no data)."""
    if not sel or not doc.ok:
        return None
    try:
        probe = wq.doc.select_all(sel).extract(_=wq.doc.attr("text")).project()
        return len(_data_rows(probe.collect(doc)))
    except Exception:  # noqa: BLE001 - a selector the engine can't run -> unknown
        return None


def _no_rows_hint(expr: Any, doc: Any) -> str:
    """A human-readable, actionable hint for why a query extracted 0 rows: either the ROW
    selector matched nothing (wrong record selector) or it matched records but no fields
    came out (wrong field selectors / missing ``.project()``). Guides the retry."""
    sel = _row_selector(expr)
    n = _selector_match_count(sel, doc)
    ops = {s.name for s in expr._plan.steps if s.kind == "get"}
    projected = "project" in ops  # did the query actually extract+project fields?
    if n == 0:
        return (
            f'Your record selector "{sel}" matched NO elements on this page, so nothing was'
            " extracted. Look again at the skeleton and pick a selector that matches ONE"
            " element per record (a repeated tag/class you can see in the skeleton)."
        )
    if n and not projected:
        # the record selector matched, but the query never extracted -- the classic "selected
        # elements, forgot to pull fields" -- so it produced 0 DATA rows.
        return (
            f'Your record selector "{sel}" matched {n} record(s), but your query only SELECTED'
            " them -- it never extracted fields, so it produced 0 data rows. Add"
            " .extract(col=wq.doc.select(...).attr(...), ...) for each field and END with"
            " .project()."
        )
    if n:
        return (
            f'Your record selector "{sel}" matched {n} element(s), but NONE of your field'
            " selectors found a value inside them. Two likely causes: (1) the record selector is"
            " too broad -- it matched a WRAPPER, not one record each. Prefer a SEMANTIC anchor: a"
            ' repeated <article>/<li>/<tr>, or [class*="product"]/[class*="post"] describing the'
            ' record -- NOT a hashed build class (e.g. ".AMTIxG_grid", ".css-1a2b3c"), which names'
            " a styling box, not a record. (2) your field selectors are right relative to the"
            " record but match nothing in it. Use the record HTML below: pick the element that"
            " wraps exactly ONE record, then field selectors you can SEE inside it."
        )
    return (
        "Your query ran but extracted 0 data rows: it MUST .select_all(<record selector>),"
        " pull each field with .extract(col=...), and END with .project() so it returns"
        " data rows -- not selected elements. Re-check your selectors against the skeleton."
    )


def _sample_record_html(expr: Any, doc: Any, *, limit: int = 900) -> str:
    """The FIRST matched record's own markup (whitespace-collapsed, trimmed) -- so a retry
    hint can SHOW the model the exact element it must extract from. This is what turns a
    vague "a field is empty" into a fixable one: the record's HTML reveals values that live
    in an ATTRIBUTE (``data-rating="Four"``, ``datetime=...``) rather than in text, so the
    model can switch ``.attr("text")`` to ``.attr("data-rating")``. Empty if unprobeable."""
    sel = _row_selector(expr)
    if not sel or not doc.ok:
        return ""
    try:
        first = wq.doc.select(sel).collect(doc)  # the first matching record element
        html = first.html() if getattr(first, "ok", False) else ""
    except Exception:  # noqa: BLE001 - a selector the engine can't run -> no sample
        return ""
    html = " ".join(html.split())
    return html[:limit] + ("…" if len(html) > limit else "")


def _nonempty(v: Any) -> bool:
    """Whether an extracted value actually carries content -- not ``None``, not blank/
    whitespace, not an empty list/dict. The test of "did the selector match content"."""
    if v is None:
        return False
    if isinstance(v, str):
        return v.strip() != ""
    if isinstance(v, (list, dict, tuple, set)):
        return len(v) > 0
    return True


def _populated_rows(rows: "list[Any]") -> "list[Any]":
    """The rows that carry AT LEAST ONE non-empty field. A row of all-empty cells means
    the record selector matched an element but every FIELD selector matched nothing (a
    guessed query, or content that isn't in this HTML) -- it is not real extracted data,
    so it must not count as a extracted row."""
    out: list[Any] = []
    for r in rows:
        if isinstance(r, dict):
            if any(_nonempty(v) for v in r.values()):
                out.append(r)
        elif _nonempty(r):
            out.append(r)
    return out


def _required_columns(brief: Brief) -> "list[str]":
    """The top-level schema field names that must be populated (non-optional)."""
    opt = {p.split(".")[0] for p in brief.optional}
    req: list[str] = []
    for f in brief.fields:
        top = f.split(".")[0]
        if top and top not in opt and top not in req:
            req.append(top)
    return req


def _required_leaf_paths(brief: Brief) -> "list[str]":
    """The required LEAF field paths (dotted). A leaf is a field with no deeper field under
    it (``price.value`` / ``price.unit`` are leaves; ``price`` is their branch). A leaf is
    optional if IT or any ancestor is marked optional. Used to validate nested extractions:
    a query that produced ``price = {"value":"","unit":""}`` populated the branch but NONE of
    its required leaves, which the old top-level check missed."""
    fields = list(brief.fields)
    opt = set(brief.optional)
    leaves = [f for f in fields if not any(g != f and g.startswith(f + ".") for g in fields)]
    req: list[str] = []
    for leaf in leaves:
        parts = leaf.split(".")
        ancestors = [".".join(parts[: i + 1]) for i in range(len(parts))]
        if not any(a in opt for a in ancestors):  # leaf or an ancestor optional -> skip
            req.append(leaf)
    return req


def _empty_required_fields(rows: "list[Any]", brief: Brief) -> "list[str]":
    """Required LEAF paths that are EMPTY (or absent) across every row -- their selectors
    matched no content, so the query is only a partial guess. Recurses into nested rows via
    :func:`_dig`, so an empty nested leaf (``price.value``) is caught even when its branch dict
    is present. Empty when the rows carry every required leaf; skipped with no schema."""
    req = _required_leaf_paths(brief)
    dict_rows = [r for r in rows if isinstance(r, dict)]
    if not req or not dict_rows:
        return []
    return [p for p in req if not any(_nonempty(_dig(r, p)) for r in dict_rows)]


def _content_hint(expr: Any, rows: "list[Any]", brief: Brief, doc: Any) -> str:
    """The retry hint when a query RAN but did not truly extract the dataset -- naming the
    specific validation that failed (record selector matched nothing / matched but fields
    are empty / a required field is empty) and warning that the content may not be in the
    HTML at all (client-rendered / iframe / shadow DOM), which a static query can't reach."""
    caveat = (
        " If the records are not visible in the skeleton at all, the page is likely rendered"
        " client-side (an SPA) or the data sits inside an iframe or shadow DOM -- a static"
        " query cannot reach it; do NOT guess selectors that are not in the skeleton."
    )
    sample = _sample_record_html(expr, doc)
    shown = (
        f"\n\nHere is the FIRST matched record's HTML -- find the missing field(s) IN IT. A "
        f"value may live in an ATTRIBUTE (e.g. data-rating=\"Four\", datetime=\"...\") rather "
        f"than in the element text: read it with .attr(\"<name>\"), not .attr(\"text\"). Do not "
        f"add fields that are genuinely absent here.\n{sample}"
        if sample else ""
    )
    if not _populated_rows(rows):  # matched a container but every field is empty (or 0 rows)
        return _no_rows_hint(expr, doc) + shown + caveat
    empty = _empty_required_fields(rows, brief)  # some required field never came out
    cols = ", ".join(f'"{c}"' for c in empty)
    return (
        f"Your query extracted rows, but the required field(s) {cols} were EMPTY on every"
        " row. Either their selector matched no element, OR it matched an element whose TEXT"
        " is empty because the value is in an attribute (use .attr(\"<name>\"))." + shown + caveat
    )


def _test_query(expr: Any, doc: Any) -> "tuple[bool, list[Any]]":
    """Run the authored query against the fetched source ``doc`` to prove it loads and
    actually EXTRACTS the dataset. The query is the document-level extraction
    (``wq.doc...``), so it collects directly against the resolved document. Returns
    ``(ran_without_error, rows)`` where ``rows`` is the extracted DATA (see
    :func:`_data_rows`) -- a query that only selects elements (no ``.project()``)
    extracts zero rows and so is not treated as a working query."""
    try:
        result = expr.collect(doc)
    except Exception:  # noqa: BLE001 - a query that can't run against the source
        return False, []
    return True, _data_rows(result)


def run_query(artifact: QueryArtifact, *, wc: WebClient) -> list[Any]:
    """Run the authored query against ALL its ``base_urls`` and concatenate the rows --
    so a dataset split across distinct URLs (``/products/cloud`` + ``/products/onprem``)
    comes back as one list. The blob is SELF-CONTAINED (reference + resolve + extraction),
    so it resolves + extracts on its own; each base just re-points the reference."""
    base_expr = from_blob(artifact.blob, wc)
    out: list[Any] = []
    for url in artifact.base_urls or []:
        try:
            result = _reroot(base_expr, url).collect()  # self-contained: no context
        except Exception:  # noqa: BLE001 - a base whose query fails contributes nothing
            continue
        out.extend(_data_rows(result))  # extracted data rows, not selected elements
    return out


#: class tokens in a selector: ``.foo`` / ``tag.foo`` / ``[class*="foo"]`` / ``[class~=foo]``.
_SEL_CLASS = __import__("re").compile(r'\.([A-Za-z_][\w-]*)|\[class[*~^$|]?=["\']?([A-Za-z_][\w-]*)')


def _record_classes(record: Any) -> "set[str]":
    """Every class token present in a record's subtree -- the real hooks a field selector could
    use, so a near-miss selector (``.widget`` for a real ``widgets``) can be repaired to one."""
    out: set[str] = set()
    try:
        el = record._element
        nodes = [el, *el.iter()] if el is not None else []
    except Exception:  # noqa: BLE001
        return out
    for node in nodes:
        cls = node.get("class") if hasattr(node, "get") else None
        if isinstance(cls, str):
            out.update(cls.split())
    return out


def _selector_hits(record: Any, selector: str) -> bool:
    """Whether ``selector`` matches at least one element inside the record (relative to it)."""
    return (_selector_match_count(selector, record) or 0) > 0


def _repair_selector(selector: str, classes: "set[str]") -> "str | None":
    """Repair a class-based selector whose class token isn't present, by swapping in the NEAREST
    real class in the record -- fixing a plural/typo/mis-transcribed high-entropy class
    (``widget``->``widgets``, ``prodcut``->``product``). Returns the repaired selector, or None
    if no token needs (or has) a close-enough real match."""
    import difflib
    import re as _re

    new = selector
    for m in _SEL_CLASS.finditer(selector):
        tok = m.group(1) or m.group(2)
        if not tok or tok in classes:
            continue  # this class token already exists -- leave it
        best, cand = 0.0, None
        for c in classes:
            r = difflib.SequenceMatcher(None, tok, c).ratio()
            # a genuine plural / one-off typo: one is a prefix of the other, BOTH are non-trivial,
            # and the lengths are close -- NOT a tiny class that happens to prefix a longer word
            # (".nodate" must NOT snap to a real ".n").
            if ((c.startswith(tok) or tok.startswith(c))
                    and min(len(tok), len(c)) >= 3 and abs(len(tok) - len(c)) <= 3):
                r = max(r, 0.9)
            if r > best:
                best, cand = r, c
        # a bit relaxed: the repaired query + its data are still validated and reviewed downstream,
        # which catches a wrong swap -- so we can afford to try a slightly looser near-match.
        if cand and best >= 0.75:  # swap the mistyped token for the real class
            new = _re.sub(rf"(?<![\w-]){_re.escape(tok)}(?![\w-])", cand, new)
    return new if new != selector else None


def _iter_field_select_args(plan: "dict[str, Any]") -> "list[dict[str, Any]]":
    """Every field-selector arg node (``{"value": "<selector>"}``) inside the extract sub-plans of
    a query plan (recursing into nested sub-extracts). NOT the top-level record selector -- only
    the FIELD selectors, which is what a repair targets. Each returned dict can be mutated in place."""
    out: list[dict[str, Any]] = []

    def walk(p: "dict[str, Any]", *, in_field: bool) -> None:
        steps = p.get("steps", [])
        for i, s in enumerate(steps):
            if in_field and s.get("kind") == "get" and s.get("name") in ("select", "select_all"):
                nxt = steps[i + 1] if i + 1 < len(steps) else None
                if nxt and nxt.get("kind") == "call" and nxt.get("args"):
                    arg = nxt["args"][0]
                    if isinstance(arg, dict) and isinstance(arg.get("value"), str):
                        out.append(arg)
            if s.get("kind") == "call":  # descend into field sub-plans (extract kwargs / args)
                for v in list(s.get("kwargs", {}).values()) + list(s.get("args", [])):
                    if isinstance(v, dict) and isinstance(v.get("plan"), dict):
                        walk(v["plan"], in_field=True)

    walk(plan, in_field=False)
    return out


def _iter_all_select_args(plan: "dict[str, Any]") -> "list[dict[str, Any]]":
    """Every ``select``/``select_all`` selector arg node in a plan -- the record selector AND all
    field selectors (recursing into sub-extracts). Each dict can be mutated in place."""
    out: list[dict[str, Any]] = []

    def walk(p: "dict[str, Any]") -> None:
        steps = p.get("steps", [])
        for i, s in enumerate(steps):
            if s.get("kind") == "get" and s.get("name") in ("select", "select_all"):
                nxt = steps[i + 1] if i + 1 < len(steps) else None
                if nxt and nxt.get("kind") == "call" and nxt.get("args"):
                    arg = nxt["args"][0]
                    if isinstance(arg, dict) and isinstance(arg.get("value"), str):
                        out.append(arg)
            if s.get("kind") == "call":
                for v in list(s.get("kwargs", {}).values()) + list(s.get("args", [])):
                    if isinstance(v, dict) and isinstance(v.get("plan"), dict):
                        walk(v["plan"])

    walk(plan)
    return out


#: the strict CSS child combinator, with any surrounding whitespace.
_CHILD_COMBINATOR = __import__("re").compile(r"\s*>\s*")


def _normalize_selectors(expr: Any) -> Any:
    """Make a loaded query's CSS selectors more robust: replace the strict child combinator ``>``
    with a descendant space -- a direct-child selector (``ul > li``) breaks the moment a wrapper
    is inserted, whereas the descendant form (``ul li``) still matches, and for extraction the
    two almost always mean the same set. XPath selectors (starting with ``/`` // ``.//``) are left
    untouched. Returns the same expr if nothing changed, else a rebuilt one."""
    import copy as _copy

    plan = _copy.deepcopy(expr._plan.model_dump(mode="json"))
    changed = False
    for arg in _iter_all_select_args(plan):
        sel = arg["value"]
        if isinstance(sel, str) and ">" in sel and not sel.lstrip().startswith(("/", ".//")):
            fixed = _CHILD_COMBINATOR.sub(" ", sel).strip()
            if fixed != sel:
                arg["value"] = fixed
                changed = True
    if not changed:
        return expr
    from ..query.expr import Expr
    from ..query.plan import Plan

    return Expr(Plan.model_validate(plan), expr._client)


def _repair_query(expr: Any, doc: Any) -> Any:
    """Repair a query whose FIELD selectors have near-miss class typos: for each field selector
    that matches nothing inside a matched record, swap the mistyped class for the nearest real one
    present in the record (see :func:`_repair_selector`), rebuild the query, and hand it back for
    re-validation. Returns a repaired Expr, or None if nothing safe to repair. Deterministic and
    conservative -- only high-confidence class swaps that then actually match are applied."""
    import copy as _copy

    row_sel = _row_selector(expr)
    if not row_sel or not getattr(doc, "ok", False):
        return None
    try:
        record = wq.doc.select(row_sel).collect(doc)  # the first matched record
    except Exception:  # noqa: BLE001
        return None
    if not getattr(record, "ok", False):
        return None
    classes = _record_classes(record)
    if not classes:
        return None
    plan = _copy.deepcopy(expr._plan.model_dump(mode="json"))
    changed = False
    for arg in _iter_field_select_args(plan):
        sel = arg["value"]
        if _selector_hits(record, sel):
            continue  # this field selector already matches -- nothing to fix
        fixed = _repair_selector(sel, classes)
        if fixed and _selector_hits(record, fixed):  # the repair actually matches now
            log.info("    repaired field selector %r -> %r", sel, fixed)
            arg["value"] = fixed
            changed = True
    if not changed:
        return None
    from ..query.expr import Expr
    from ..query.plan import Plan

    return Expr(Plan.model_validate(plan), expr._client)


def _short_fail_reason(expr: Any, rows: "list[Any]", brief: Brief, doc: Any) -> str:
    """A ONE-LINE reason a query didn't extract cleanly -- for the log (the full, multi-line
    hint goes to the model, not the console). Keeps the retry trace legible."""
    good = _populated_rows(rows)
    if not good:
        n = _selector_match_count(_row_selector(expr), doc)
        return f"0 rows (record selector matched {n if n is not None else '?'})"
    empty = _empty_required_fields(good, brief)
    if empty:
        return f"required field(s) {', '.join(empty)} empty on every row"
    return f"{len(good)} row(s) but incomplete"


def _artifact_from(
    expr: Any, doc: Any, brief: Brief, candidate_url: str,
    resolve: "Resolve | None", bases: "list[str]",
) -> "tuple[QueryArtifact, list[Any]]":
    """Test one authored query against the source and build its :class:`QueryArtifact` (the
    self-contained, runnable blob + validation verdict + timeliness flag). Shared by the
    one-shot and staged authors. Returns ``(artifact, extracted_rows)``."""
    tested, rows = _test_query(expr, doc) if doc.ok else (False, [])
    good = _populated_rows(rows)
    missing = _empty_required_fields(good, brief)  # required leaves empty on every row
    tnote, stale = _timeliness(good, brief)  # over ALL rows; a FLAG, never a ship blocker
    exe = _executable_query(expr, candidate_url, resolve)  # self-contained + runnable
    art = QueryArtifact(
        blob=exe.to_blob(),
        describe=exe.describe(),
        plan=exe._plan.model_dump(mode="json"),
        tested=tested,
        complete=bool(tested and good and not missing),  # every required leaf populated
        row_count=len(good),
        sample=list(good[:5]),
        resolve=(resolve.model_dump(mode="json") if resolve is not None else {}),
        base_urls=bases,
        timeliness=tnote,
        stale=stale,
    )
    return art, rows


class _Author:
    """Drives the query-authoring turns. The PAGE (guide + skeleton + brief) is the OPENING
    message; each retry sends only the short feedback. If the model keeps a conversation (an
    :class:`LlmClient` exposes ``.conversation()``), the page stays in context and is re-read
    from cache instead of re-submitted every attempt; a plain ``Callable[[str], str]`` has no
    memory, so the page is re-sent with each turn (the fallback -- same behaviour as before)."""

    def __init__(self, llm: LLM, opening: str) -> None:
        conv = getattr(llm, "conversation", None)
        self._chat: Any = conv() if callable(conv) else None
        self._llm = llm
        self._opening = opening
        self._opened = False

    def send(self, follow_up: "str | None" = None) -> str:
        if self._chat is not None:  # stateful: the page once, then just the follow-up
            msg = self._opening if not self._opened else (follow_up or "Try again.")
            self._opened = True
            return str(self._chat.send(msg))
        # stateless callable: no memory -> the page must ride along every turn
        return self._llm(self._opening if not follow_up else f"{self._opening}\n\n{follow_up}")


def _should_retry_for_recency(art: QueryArtifact, attempt: int, tries: int) -> bool:
    """A COMPLETE query whose newest data looks stale is probably scoped to an ARCHIVED
    period (a hidden year tab, an old paginated page). Worth one more try for the most
    recent data -- but never a ship blocker, so only while attempts remain."""
    return art.stale and attempt < tries - 1


def _recency_follow_up(art: QueryArtifact) -> str:
    """The feedback that pushes the model toward the MOST RECENT data + the hidden-tabs pattern."""
    return (
        f"That query is COMPLETE, but the most recent data looks MISSING: {art.timeliness}. "
        "The records you selected are probably an ARCHIVED period. Sites keep the CURRENT period "
        "behind a control: YEAR TABS (an older year shows by default), a 'Latest' vs 'Archive' "
        "toggle, a category filter, or a paginated first page. In the skeleton look for clickable "
        "tab/filter controls (marked '← clickable'), pagination, and records marked '[xhr]' "
        "(loaded on demand). Re-write the query to capture the MOST RECENT records -- pick a "
        "record selector that is NOT scoped to one archived tab (select across all of them, or "
        "the current/latest tab). Is there more recent data than what you selected?"
    )


def write_query(
    candidate_url: str,
    brief: Brief,
    *,
    wc: WebClient,
    llm: LLM,
    browser: BrowserMode = "auto",
    paginated: bool = False,
    retries: int = 4,
    extra_urls: Sequence[str] = (),
    resolve: "Resolve | None" = None,
    doc: Any = None,
    recency: str = "",
) -> QueryArtifact | None:
    """Have the model author the DOCUMENT-level extraction from the page skeleton, test
    it against the fetched source (``from_blob`` + run -> it must extract DATA rows), and
    return the best :class:`QueryArtifact`. The stored ``blob`` is the SELF-CONTAINED
    executable query -- the extraction wrapped in ``reference(url).resolve(...)`` so it
    runs as is (:func:`_executable_query`). Retries with feedback on an invalid or
    non-extracting query -- the page is sent ONCE and each retry is a short follow-up when the
    model keeps a conversation (see :func:`_author`), else re-sent. ``paginated`` tells the
    author to capture the next-page link; ``extra_urls`` are further base URLs the same query
    also runs against; ``resolve`` bakes the fetch policy (browser tier) into the executable
    query. ``doc`` is the already-fetched source (from the flag-read step) -- reused so we
    don't re-fetch it."""
    if doc is None:
        doc = wc.fetch(candidate_url, browser=browser, optional=True)
    skeleton = _skeleton_for(doc) if doc.ok else ""
    prompt = _query_prompt(brief, skeleton, paginated=paginated, recency=recency)
    bases = [candidate_url, *extra_urls]
    best: QueryArtifact | None = None
    best_complete: QueryArtifact | None = None  # a complete-but-STALE fallback (recency retries)
    author = _Author(llm, prompt)  # the page rides in the OPENING; retries send only feedback
    follow_up: "str | None" = None
    tries = retries + 1
    for attempt in range(tries):
        tag = f"    query {attempt + 1}/{tries}"
        try:
            reply = author.send(follow_up)
        except LlmError as exc:  # a bad-request / exhausted-retry API error
            log.warning("%s: LLM call failed (%s)", tag, exc)
            break
        try:
            expr = _parse_query(reply)  # load the written wq.doc chain (or a raw blob)
        except Exception as exc:  # noqa: BLE001 - unparsable query code -> retry with feedback
            log.info("%s: reply was not a valid query (%s) — retrying", tag, exc)
            log.debug("      unparseable reply: %.200r", reply.strip())  # the detail, at debug
            follow_up = ("Your previous reply was not a valid query. Reply with ONLY the query"
                         " code -- a single wq.doc... chain, nothing else.")
            continue
        # a real extraction MUST select the records -- a query with no select_all/select
        # can't extract anything, so reject it before it looks like a 0-row "success".
        ops = {s.name for s in expr._plan.steps if s.kind == "get"}
        if not ({"select", "select_all"} & ops):
            log.info("%s: no record selection — retrying", tag)
            follow_up = ("Your previous query had NO selection so it extracts nothing. You MUST"
                         " select the repeating record with .select_all(...), pull each field with"
                         " .extract(col=...), and END with .project(). Re-write it.")
            continue
        art, rows = _artifact_from(expr, doc, brief, candidate_url, resolve, bases)
        if art.complete:
            if not _should_retry_for_recency(art, attempt, tries):
                note = f" (STALE flag: {art.timeliness})" if art.stale else ""
                log.info("%s: ✓ complete%s — %d row(s)", tag, note, art.row_count)
                return art
            best_complete = art  # complete but stale: keep it, but push for the most recent data
            log.info("%s: complete but STALE — retrying for the most recent data (%s)",
                     tag, art.timeliness)
            follow_up = _recency_follow_up(art)
            continue
        # AUTO-REPAIR a near-miss field selector (a one-char class typo: widget vs widgets) by
        # swapping in the nearest real class present in the record, then re-validate.
        repaired = _repair_query(expr, doc)
        if repaired is not None:
            rart, _rrows = _artifact_from(repaired, doc, brief, candidate_url, resolve, bases)
            if rart.complete and not _should_retry_for_recency(rart, attempt, tries):
                note = f" (STALE flag: {rart.timeliness})" if rart.stale else ""
                log.info("%s: ✓ complete after auto-repairing a selector%s — %d row(s)",
                         tag, note, rart.row_count)
                return rart
            if rart.complete and rart.stale:  # repaired but stale -> keep as fallback, push recency
                best_complete = rart
                log.info("%s: repaired + complete but STALE — retrying for recent (%s)",
                         tag, rart.timeliness)
                follow_up = _recency_follow_up(rart)
                continue
            art = rart if rart.row_count > art.row_count else art  # keep the better fallback
        best = best or art  # keep the first rebuildable one as a fallback (NOT complete)
        # a concise reason on the console; the full, multi-line diagnostic hint goes to the model.
        log.info("%s: %s — retrying", tag, _short_fail_reason(expr, rows, brief, doc))
        follow_up = (
            "That query did not extract the dataset. Produce a MATERIALLY DIFFERENT query --"
            " change the .select_all(...) RECORD selector to a more semantic anchor, don't just"
            f" tweak the fields.\n\nYour previous query was:\n{expr.describe()}\n\n"
            f"{_content_hint(expr, rows, brief, doc)}"
        )
    # a complete-but-stale query (recency retries didn't find fresher data) beats an incomplete
    # one: it's a working query, and staleness is a FLAG for the human review, not a blocker.
    if best_complete is not None:
        log.info("    keeping the complete query with a STALE flag (%s)", best_complete.timeliness)
        return best_complete
    if best is None:  # every attempt failed to author a usable query -- say so loudly
        log.warning("    could not author a working query in %d attempt(s)", tries)
    return best


# --------------------------------------------------------------------------- #
# 8. review  (LLM meta-review: grade the run's choices)
# --------------------------------------------------------------------------- #


def _as_bool(v: Any) -> bool:
    """Coerce a model's truthy/falsey field to bool, treating the STRING tokens a cheap model
    emits ("false"/"no"/"0"/"") as False -- so ``"pass": "false"`` isn't read as truthy."""
    if isinstance(v, str):
        return v.strip().lower() not in ("false", "no", "0", "", "none", "null")
    return bool(v)


def _review_from_json(stage: str, data: Any) -> "Review | None":
    """Build a :class:`Review` from a model's JSON judgement (``verdict`` / ``score`` /
    ``issues`` / ``summary``). ``None`` if the model gave nothing usable."""
    if not isinstance(data, dict):
        return None
    issues = data.get("issues") or []
    if not isinstance(issues, list):
        issues = [str(issues)]
    try:
        score = int(data.get("score") or 0)
    except (TypeError, ValueError):
        score = 0
    return Review(
        stage=stage,
        verdict=str(data.get("verdict") or ""),
        passed=_as_bool(data.get("pass", True)),  # absent -> default pass; a stringy "false" is False
        score=max(0, min(10, score)),
        issues=[str(i) for i in issues][:10],
        summary=str(data.get("summary") or ""),
    )


def _page_lines(crawl: Any, limit: int = 40) -> str:
    """The crawled pages as ``url [tier] flags — title`` lines (for a review prompt)."""
    out: list[str] = []
    for card in (getattr(crawl, "pages", []) or [])[:limit]:
        tier = getattr(card, "final_tier", "static") or "static"
        flags = ", ".join(getattr(card, "flags", []) or []) or "-"
        title = (getattr(card, "title", "") or "").strip()
        out.append(f"{card.final_url or card.url} [{tier}; {flags}]" + (f" — {title}" if title else ""))
    fails = [f"{f.url} ({f.reason})" for f in (getattr(crawl, "failures", []) or [])[:15]]
    if fails:
        out.append("failed: " + "; ".join(fails))
    return "\n".join(out)


def review_crawl(artifacts: _RunArtifacts, brief: Brief, *, llm: LLM) -> "Review | None":
    """Grade the crawl: did it reach the pages likely to hold the dataset, and was it
    complete (not too shallow, not off down irrelevant paths)?"""
    if artifacts.crawl is None:
        return None
    data = _ask_json(llm, render_prompt(
        "review_crawl",
        description=brief.description, fields_line=_fields_line(brief),
        seeds="\n".join(s.url for s in artifacts.seeds) or "(none)",
        pages=_clip(_page_lines(artifacts.crawl), _MAX_PAGES_CHARS, "crawled pages") or "(none)",
    ))
    return _review_from_json("crawl", data)


def review_select(result: OnboardingResult, artifacts: _RunArtifacts, brief: Brief, *, llm: LLM) -> "Review | None":
    """Grade the selection: from the crawled pages, were the right URLs picked as
    candidates, and was the best source chosen to scrape?"""
    if not artifacts.candidates:
        return None
    chosen = result.evaluation.url if result.evaluation else "(none chosen)"
    cands = "\n".join(f"{c.url} [{c.tier}] {c.note}".rstrip() for c in artifacts.candidates)
    data = _ask_json(llm, render_prompt(
        "review_select",
        description=brief.description, fields_line=_fields_line(brief),
        pages=_clip(_page_lines(artifacts.crawl), _MAX_PAGES_CHARS, "crawled pages") or "(none)",
        candidates=_clip(cands, _MAX_LISTING_CHARS, "candidates"),
        chosen=chosen,
    ))
    return _review_from_json("select", data)


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


def review_query(result: OnboardingResult, artifacts: _RunArtifacts, brief: Brief, *, llm: LLM) -> "Review | None":
    """Grade the authored query: does the output table hold data matching the brief, are
    the selectors targeting the relevant parts of the page, and do they capture ALL the
    records on the page (completeness)?"""
    q = result.query
    if q is None:
        return None
    doc = artifacts.query_doc
    skeleton = _skeleton_for(doc) if (doc is not None and doc.ok) else "(unavailable)"
    sample = json.dumps(list(q.sample)[:8], default=str, indent=2)
    # timeliness was assessed over ALL rows at authoring time (stored on the artifact); reuse it
    # rather than recomputing from the 5-row sample (which can miss the newest item).
    tnote = q.timeliness or "(no date field to assess timeliness)"
    data = _ask_json(llm, render_prompt(
        "review_query",
        description=brief.description, fields_line=_fields_line(brief),
        query=q.describe, row_count=str(q.row_count), tested=str(q.tested),
        sample=_clip(sample, _MAX_LISTING_CHARS, "sample rows"),
        timeliness=tnote,
        # completeness is KEPT but DISABLED by default -- flip _CHECK_COMPLETENESS to gate on it
        completeness=(_COMPLETENESS_BLOCK if _CHECK_COMPLETENESS else _COMPLETENESS_OFF),
        skeleton=skeleton,
    ))
    review = _review_from_json("query", data)
    if q.stale:  # surface staleness as a FLAG on the review (never abandons the query)
        review = review or Review(stage="query", verdict="partial")
        review.passed = False  # recorded as "flagged" by _note_review, not a hard gate
        if q.timeliness and q.timeliness not in review.issues:
            review.issues = [q.timeliness, *review.issues][:10]
        if not review.summary:
            review.summary = "the most recent data may be missing (timeliness flag)"
    return review


def review_failure(result: OnboardingResult, artifacts: _RunArtifacts, brief: Brief, *, llm: LLM) -> "Review | None":
    """Diagnose a failed run: from the trace + how far it got, name the most likely cause
    and what would fix it. Included in the summary so a human sees WHY it failed."""
    reached = (
        f"seeds={len(artifacts.seeds)}, crawled={len(getattr(artifacts.crawl, 'pages', []) or [])}, "
        f"candidates={len(artifacts.candidates)}, evaluated={'yes' if result.evaluation else 'no'}, "
        f"query={'yes' if result.query else 'no'}"
        + (f" ({result.query.row_count} rows)" if result.query else "")
    )
    data = _ask_json(llm, render_prompt(
        "review_failure",
        description=brief.description, fields_line=_fields_line(brief),
        reason=result.reason or "(unknown)",
        reached=reached,
        trace="\n".join(result.steps[-25:]) or "(no trace)",
    ))
    return _review_from_json("failure", data)


def _note_review(result: OnboardingResult, review: "Review | None") -> None:
    """Record a stage review as INFORMATION for the human, without abandoning the run. The
    review (an LLM judgement, or the deterministic timeliness assessment) is appended to
    ``result.reviews`` and traced, but a non-passing review NO LONGER fails the pipeline: it is
    a FLAG, not a hard gate. Whether a run ships is decided by the DETERMINISTIC extraction
    result (``q.complete`` -- real rows, every required field populated), not by a model's
    opinion or a timeliness note -- so a useful warning ("the data looks stale", "the crawl
    was thin") is surfaced for review instead of throwing away a working query. ``None`` (the
    review was skipped) records nothing."""
    if review is None:
        return
    result.reviews.append(review)
    _trace(result, "%s review: %s%s", review.stage,
           "passed" if review.passed else "flagged",
           f" — {review.summary}" if review.summary else "")


def diagnose_failure(result: OnboardingResult, artifacts: _RunArtifacts, *, brief: Brief, llm: LLM) -> None:
    """On a failed run (whether a stage review gated it or a stage errored), add the
    failure review's diagnosis to ``result.reviews`` so the summary says WHY it failed."""
    review = review_failure(result, artifacts, brief, llm=llm)
    if review is not None:
        result.reviews.append(review)


# --------------------------------------------------------------------------- #
# The orchestrator.
# --------------------------------------------------------------------------- #


def onboard_company(
    company: str,
    brief: Brief,
    *,
    wc: WebClient,
    llm: LLM,
    search: SearchFn,
    max_pages: int = 20,
    browser: bool = True,
    budget: Budget | None = None,
    review: bool = False,
) -> OnboardingResult:
    """Run the whole pipeline for one company: search -> crawl -> select -> evaluate
    -> write the reference, resolve, and query for the best source found.

    Pass a :class:`~webclient.pipelines.llm.Budget` to cap LLM spend for this run: when
    an :class:`~webclient.pipelines.llm.LlmClient` is the injected ``llm`` the budget is
    attached to it, and if the cap is hit mid-pipeline the run stops and reports
    ``ok=False`` / ``reason="llm budget exceeded"`` instead of raising to the caller.

    ``review=True`` runs the meta-review stage after the pipeline: the model grades the
    run's choices (the crawl, the URL selection, the authored query -- and, on a failure,
    diagnoses what went wrong). The reviews land on ``result.reviews`` and in the summary.
    Off by default (it costs extra LLM calls)."""
    _ensure_logging()  # progress is always visible
    result = OnboardingResult(company=company, brief=brief)
    artifacts = _RunArtifacts()
    # Thread the cap into an LlmClient so its per-call spend is enforced. A plain
    # callable llm (e.g. a test stub) carries no cost, so there is nothing to cap.
    if budget is not None and isinstance(llm, LlmClient):
        llm.budget = budget
    try:
        _onboard_company(
            company, brief, result, artifacts,
            wc=wc, llm=llm, search=search, max_pages=max_pages, browser=browser,
            review=review,  # stage reviews are integral + gating, run inline (see _onboard_company)
        )
        if review and not result.ok:  # diagnose WHY it failed (a gate, or a stage error)
            diagnose_failure(result, artifacts, brief=brief, llm=llm)
    except BudgetExceeded:
        result.ok = False
        result.reason = result.reason or "llm budget exceeded"
    if isinstance(llm, LlmClient):
        result.cost_usd = llm.spent_usd
    _summarize(result)  # always print the end-of-run summary
    return result


def _onboard_company(
    company: str,
    brief: Brief,
    result: OnboardingResult,
    artifacts: _RunArtifacts,
    *,
    wc: WebClient,
    llm: LLM,
    search: SearchFn,
    max_pages: int,
    browser: bool,
    review: bool = False,
) -> OnboardingResult:
    def note(msg: str, *a: Any) -> None:  # trace with the running spend kept current
        if isinstance(llm, LlmClient):
            result.cost_usd = llm.spent_usd
        _trace(result, msg, *a)

    note("searching the web for seeds")
    seeds = search_web(brief, company, search=search, llm=llm)
    artifacts.seeds = list(seeds)
    if not seeds:
        result.reason = "no search seeds"
        return result
    note("%d seed(s); crawling for the dataset", len(seeds))
    crawl = crawl_from_seeds(
        seeds, brief, wc=wc, llm=llm, company=company, max_pages=max_pages, browser=browser
    )
    artifacts.crawl = crawl
    note("crawled %d page(s), %d failed", len(crawl.pages), len(crawl.failures))
    # the crawl review is a FLAG for the human, not a gate -- record it and press on.
    if review:
        _note_review(result, review_crawl(artifacts, brief, llm=llm))
    candidates = select_candidates(
        crawl, brief, llm=llm, seed_urls=[s.url for s in artifacts.seeds if s.url]
    )
    artifacts.candidates = list(candidates)
    if not candidates:
        result.reason = "no candidate pages"
        return result
    note("%d candidate(s); evaluating best-first", len(candidates))
    evaluation = evaluate_candidates(
        candidates, brief, wc=wc, llm=llm, browser=_mode(browser)
    )
    if evaluation is None or not evaluation.dataset_present:
        result.reason = "no usable source found"
        result.evaluation = evaluation
        return result
    result.evaluation = evaluation
    note(
        "chose %s (queryable=%s, scrapability=%d)",
        evaluation.url, evaluation.is_queryable, evaluation.scrapability,
    )
    # the selection review is a FLAG for the human, not a gate -- record it and press on.
    if review:
        _note_review(result, review_select(result, artifacts, brief, llm=llm))
    # -- the flag-driven decision cascade for the chosen source, in order ----------
    # (1) reference + query URL: the same source -- a same-origin data API only when the page is
    # an SPA shell backed by it, otherwise the page itself (see _source_url).
    result.reference = write_reference(evaluation, wc=wc)
    query_url = _source_url(evaluation)
    doc = wc.fetch(query_url, browser=_mode(browser), optional=True)
    artifacts.query_doc = doc
    flags = _read_flags(doc) if doc.ok else {}
    # (2) a login wall on the source itself -> no query reaches the data; stop.
    if flags.get("login_required") is not None and flags["login_required"].present:
        result.reason = "the source requires login"
        return result
    # (3) resolve policy: spa -> browser, anti_bot_triggered -> proxy/stealth.
    result.resolve = write_resolve(list(flags.values()))
    fired = [n for n, f in flags.items() if f.present]
    note("flags fired: %s; authoring the query", ", ".join(fired) or "none")
    # (4) query: authored from the skeleton, told to page when the source paginates. The source
    # was already fetched above (for the flags) -- reuse that Document so write_query doesn't
    # re-fetch (a browser/proxy re-fetch is real budget + latency, and can drift the skeleton).
    result.query = write_query(
        query_url, brief, wc=wc, llm=llm, browser=_mode(browser),
        paginated=evaluation.has_pagination, resolve=result.resolve,
        doc=doc if doc.ok else None,
        recency=_recency_guidance(evaluation),  # sort order + where the most recent records are
    )
    if isinstance(llm, LlmClient):
        result.cost_usd = llm.spent_usd
    # a real success EXTRACTS data with every required field: a query that ran but produced
    # 0 rows, or left a required field empty on every row, is NOT ok (a fallback `best`
    # artifact is kept for the summary but is marked not-complete).
    q = result.query
    result.ok = q is not None and q.complete and q.row_count > 0
    if result.ok:
        result.reason = ""
    elif q is not None and q.row_count > 0:
        result.reason = "authored query is missing required field(s)"
    elif q is not None:
        result.reason = "authored query extracted 0 rows"
    else:
        result.reason = "could not author a query"
    if q is not None:
        note("query authored (tested=%s, %d row[s])", q.tested, q.row_count)
    # the query review + the deterministic timeliness assessment are FLAGS for the human
    # (recorded on result.reviews), not gates: a working query is never discarded because a
    # model graded it low or the data looks stale.
    if review and q is not None:
        _note_review(result, review_query(result, artifacts, brief, llm=llm))
    return result


def onboard(
    companies: Sequence[str],
    brief: Brief,
    *,
    wc: WebClient,
    llm: LLM,
    search: SearchFn,
    max_pages: int = 20,
    browser: bool = True,
    budget: Budget | None = None,
    review: bool = False,
) -> list[OnboardingResult]:
    """Onboard several companies for the same brief (sequentially, one crawl each).

    A shared ``budget`` caps LLM spend across the WHOLE run: once it is exhausted the
    remaining companies report ``ok=False`` / ``reason="llm budget exceeded"``.
    ``review=True`` runs the meta-review stage for each company (see :func:`onboard_company`)."""
    return [
        onboard_company(
            c, brief, wc=wc, llm=llm, search=search, max_pages=max_pages,
            browser=browser, budget=budget, review=review,
        )
        for c in companies
    ]
