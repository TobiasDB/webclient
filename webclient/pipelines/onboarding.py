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
        no description); ``look`` / ``ignore`` (NL guide lines); ``crawl`` (a mapping of
        pipeline crawl overrides -- ``max_pages`` / ``depth`` / ``rounds`` / ``browser``);
        ``description`` (else the body)."""
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
    sort_order: str | None = None
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
    ``from_blob``; ``plan`` is the same chain as a plan dict (``from_plan``-loadable /
    the wire form). It is TESTED at authoring time -- run against the source -- so
    ``tested`` / ``row_count`` / ``sample`` report whether it actually extracts rows."""

    blob: str  # the portable lazy-query blob (rebuildable with from_blob)
    describe: str  # a readable one-line rendering of the chain
    plan: dict[str, Any] = {}  # the plan dict (from_plan-loadable; the wire form)
    tested: bool = False  # did it run against the source without error?
    row_count: int = 0  # how many rows it produced when tested
    sample: list[Any] = []  # up to 5 produced rows (as data), shown as a table
    #: the source URLs this one query runs against, unioned. Usually one, but a dataset
    #: split across distinct URLs (e.g. /products/cloud + /products/onprem -- NOT
    #: pagination) lists them all; :func:`run_query` resolves the query per base and
    #: concatenates the rows.
    base_urls: list[str] = []


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
    """Emit one pipeline step -- always printed (see :func:`_ensure_logging`) -- and
    append it to the result's ``steps`` trace, prefixed with the running LLM spend so
    the cost is visible as it accrues."""
    rendered = message % args if args else message
    result.steps.append(rendered)
    log.info("[$%.4f] %s: %s", result.cost_usd, result.company, rendered)


def _cell(value: Any) -> str:
    """One table cell -- JSON for a nested value, truncated so the table stays legible."""
    s = json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else str(value)
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
        lines.append(
            "  scores:    "
            + f"scrapability {ev.scrapability}/10, queryable={ev.is_queryable}, "
            + f"present={ev.dataset_present}, complete={ev.completeness or '?'}, "
            + f"paginated={ev.has_pagination}, filters={ev.has_filters}, "
            + f"subset={ev.dataset_is_subset}, interactive={ev.interactive}"
        )
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
    """The first balanced JSON object/array in ``text`` (models like to wrap it in
    prose). Falls back to the whole stripped string."""
    t = _strip_fences(text)
    starts = [i for i in (t.find("{"), t.find("[")) if i != -1]
    if not starts:
        return t
    start = min(starts)
    open_ch = t[start]
    close_ch = "}" if open_ch == "{" else "]"
    depth = 0
    for i in range(start, len(t)):
        if t[i] == open_ch:
            depth += 1
        elif t[i] == close_ch:
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
        log.debug("LLM prompt ~%d tokens", len(ask) // 4)
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
    "pagination", "forms", "buttons",
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
    with DDGS() as ddgs:
        rows = ddgs.text(query, max_results=k) or []
    return [
        SearchHit(
            url=str(r.get("href") or r.get("url") or ""),
            title=str(r.get("title") or ""),
            snippet=str(r.get("body") or r.get("snippet") or ""),
        )
        for r in rows
        if r.get("href") or r.get("url")
    ]


#: appended to the search-query prompt on a retry, when the first results were a look-alike
_DISAMBIGUATE = (
    "\n\nThe previous search returned a DIFFERENT company with a similar name, not this one."
    " Rewrite the query so it is UNAMBIGUOUS for this exact company -- add a distinguishing"
    " word (its industry, headquarters, 'official', or a term from the description)."
)


def _search_query(brief: Brief, company: str, llm: "LLM | None", *, disambiguate: bool = False) -> str:
    """The web-search query for ``company`` -- the model crafts a sharper one (told to
    disambiguate a look-alike name on a retry); falls back to ``"<company> <brief>"``."""
    query = f"{company} {brief.description}".strip()
    if llm is not None:
        prompt = render_prompt(
            "search_query", company=company, description=brief.description,
            fields_line=_fields_line(brief),
        ) + (_DISAMBIGUATE if disambiguate else "")
        crafted = llm(prompt).strip().splitlines()
        if crafted and crafted[0].strip():
            query = crafted[0].strip()
    return query


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
    """Seed URLs for ``company`` + ``brief``. The query is ``"<company> <brief>"`` by
    default; pass ``llm`` to craft a sharper query AND to verify each result really belongs
    to ``company`` (dropping look-alike companies with a similar name). If the whole first
    result set is the wrong company, the search retries ONCE with a disambiguating query."""
    for attempt in range(2):
        query = _search_query(brief, company, llm, disambiguate=(attempt > 0))
        log.info("    search query: %r", query)
        seeds = [Seed(url=h.url, title=h.title, why=h.snippet) for h in search(query, k) if h.url]
        kept = _seeds_for_company(seeds, company, brief, llm)
        if kept:
            if len(kept) < len(seeds):
                log.info("    %d/%d result(s) belong to %s", len(kept), len(seeds), company)
            return kept
        if seeds:  # results came back but none were this company -- try a stricter query
            log.info("    no result belongs to %s -- retrying the search, stricter", company)
    return []


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
            log.info("    model picked no seeds -- fetching the filtered seeds")
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
        tier = getattr(card, "final_tier", "static") or "static"
        transport = "http" if tier == "static" else tier  # static tier == a plain HTTP fetch
        flags = ", ".join(getattr(card, "flags", []) or []) or "none"
        log.info("    crawl [%s] %s  (via %s; signals: %s)",
                 card.status_code, card.final_url or card.url, transport, flags)
    for fail in crawl.failures[seen_fails:]:
        log.info("    crawl [%s] %s  (%s)", fail.status_code or "x", fail.url, fail.reason)
    return len(crawl.pages), len(crawl.failures)


# --------------------------------------------------------------------------- #
# 3. select_candidates
# --------------------------------------------------------------------------- #


def select_candidates(crawl: Any, brief: Brief, *, llm: LLM) -> list[Candidate]:
    """Rank the crawled pages into must / should / could-evaluate candidates by
    scrapability + likely relevance to the dataset."""
    # crawl.pages are lean PageCards by default (url / title / flags already projected)
    # hard-ban documentation pages: even if one was fetched (a seed / a stray pick), it
    # is never a scrapable dataset, so it can't become a candidate.
    pages = [
        {"url": p.final_url or p.url, "title": p.title, "flags": p.flags}
        for p in crawl.pages if not _is_docs_url(p.final_url or p.url)
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
    skeleton = _clip(doc.skeleton(max_lines=_FULL_SKELETON), _MAX_SKELETON_CHARS, "skeleton", kind=("json" if doc.kind == "json" else "html"))
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
    Prefers a queryable source when scores tie."""
    best: CandidateEval | None = None
    for c in candidates:
        ev = evaluate_candidate(c, brief, wc=wc, llm=llm, browser=browser)
        rank = (ev.is_queryable, ev.scrapability)
        if best is None or rank > (best.is_queryable, best.scrapability):
            best = ev
        if ev.usable and ev.is_queryable:
            return ev  # a queryable source clean enough to scrape -- stop early
    return best


# --------------------------------------------------------------------------- #
# 5. write_reference  (deterministic given the candidate)
# --------------------------------------------------------------------------- #


def write_reference(evaluation: CandidateEval, *, wc: WebClient) -> Reference:
    """The lazy ``Reference`` for the chosen source -- deterministic given the
    candidate. When the SPA is backed by a same-origin data API (``api_endpoint``),
    root the Reference at the ENDPOINT: querying the API beats scraping the rendered
    page. Otherwise the candidate URL (query already baked in by the crawl)."""
    return wc.ref(evaluation.api_endpoint or evaluation.url)


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


def _query_prompt(brief: Brief, skeleton: str, *, paginated: bool = False) -> str:
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
    )


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
    (no LLM) supplies the reference + resolve. The browser tier comes from the resolve
    policy (proxy/antibot are transport concerns a lazy ``.resolve()`` can't encode)."""
    from ..query.expr import Expr
    from ..query.plan import Plan

    tier = resolve.browser.when if (resolve is not None and resolve.browser is not None) else None
    rooted = wq.reference(url).resolve(browser=tier) if tier else wq.reference(url).resolve()
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


def _parse_query(reply: str) -> Any:
    """Load the model's query. The model WRITES it as a ``wq.doc`` chain -- exactly as the
    guide documents -- and we evaluate that code into an ``Expr``, loading it as written
    rather than asking the model to hand-serialize a ``to_blob()`` JSON (which it gets
    wrong -- e.g. dropping the ``select_all`` so the query extracts nothing). A raw blob is
    still accepted as a fallback. The eval namespace is just ``wq`` with no builtins: the
    DSL records lazily, so building the query does no IO and reaches nothing but the DSL."""
    from ..query.expr import Expr

    code = _query_code(reply)
    if code.startswith("wq."):
        expr = eval(code, {"__builtins__": {}, "wq": wq})  # noqa: S307 - our DSL, restricted ns
        if not isinstance(expr, Expr):
            raise TypeError(f"query is a {type(expr).__name__}, not a wq.doc chain")
        return expr
    return from_blob(_json_blob(reply))  # fallback: the model returned a raw blob


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
    if n == 0:
        return (
            f'Your record selector "{sel}" matched NO elements on this page, so nothing was'
            " extracted. Look again at the skeleton and pick a selector that matches ONE"
            " element per record (a repeated tag/class you can see in the skeleton)."
        )
    if n:
        return (
            f'Your record selector "{sel}" matched {n} record(s), but none of your fields'
            " produced a value -- your .extract(...) FIELD selectors do not match anything"
            " inside a record, or you did not .project(). Re-check each field selector"
            " against the skeleton (they are relative to the record), and END with .project()."
        )
    return (
        "Your query ran but extracted 0 data rows: it MUST .select_all(<record selector>),"
        " pull each field with .extract(col=...), and END with .project() so it returns"
        " data rows -- not selected elements. Re-check your selectors against the skeleton."
    )


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
    """The top-level schema field names that must be populated (non-optional). The query's
    ``.extract(col=...)`` columns are named after these, so we can check each really came
    out with content."""
    opt = {p.split(".")[0] for p in brief.optional}
    req: list[str] = []
    for f in brief.fields:
        top = f.split(".")[0]
        if top and top not in opt and top not in req:
            req.append(top)
    return req


def _empty_required_fields(rows: "list[Any]", brief: Brief) -> "list[str]":
    """Required columns that are EMPTY (or absent) across every row -- their selectors
    matched no content, so the query is only a partial guess. Empty when the rows carry
    every required field. Skipped when the brief has no schema (nothing to check)."""
    req = _required_columns(brief)
    dict_rows = [r for r in rows if isinstance(r, dict)]
    if not req or not dict_rows:
        return []
    return [c for c in req if not any(_nonempty(r.get(c)) for r in dict_rows)]


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
    if not _populated_rows(rows):  # matched a container but every field is empty (or 0 rows)
        return _no_rows_hint(expr, doc) + caveat
    empty = _empty_required_fields(rows, brief)  # some required field never came out
    cols = ", ".join(f'"{c}"' for c in empty)
    return (
        f"Your query extracted rows, but the required field(s) {cols} were EMPTY on every"
        " row -- those field selectors match nothing inside a record. Re-check them against"
        " the skeleton (selectors are relative to the record)." + caveat
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
) -> QueryArtifact | None:
    """Have the model author the DOCUMENT-level extraction from the page skeleton, test
    it against the fetched source (``from_blob`` + run -> it must extract DATA rows), and
    return the best :class:`QueryArtifact`. The stored ``blob`` is the SELF-CONTAINED
    executable query -- the extraction wrapped in ``reference(url).resolve(...)`` so it
    runs as is (:func:`_executable_query`). Retries with feedback on an invalid or
    non-extracting query. ``paginated`` tells the author to capture the next-page link;
    ``extra_urls`` are further base URLs the same query also runs against;
    ``resolve`` bakes the fetch policy (browser tier) into the executable query."""
    doc = wc.fetch(candidate_url, browser=browser, optional=True)
    skeleton = _clip(doc.skeleton(max_lines=_FULL_SKELETON), _MAX_SKELETON_CHARS, "skeleton", kind=("json" if doc.kind == "json" else "html")) if doc.ok else ""
    prompt = _query_prompt(brief, skeleton, paginated=paginated)
    bases = [candidate_url, *extra_urls]
    best: QueryArtifact | None = None
    ask = prompt
    for _ in range(retries + 1):
        try:
            reply = llm(ask)
        except LlmError as exc:  # a bad-request / exhausted-retry API error
            log.warning("query authoring LLM call failed: %s", exc)
            break
        try:
            expr = _parse_query(reply)  # load the written wq.doc chain (or a raw blob)
        except Exception as exc:  # noqa: BLE001 - unparsable query code -> retry with feedback
            # surface WHAT the model said so an all-unparseable run is diagnosable, not a
            # silent "could not author a query"
            log.info("    query reply not parseable (%s) -- retrying; reply: %.160r", exc, reply.strip())
            ask = prompt + "\n\nYour previous reply was not a valid query. Reply with ONLY the query code -- a single wq.doc... chain, nothing else."
            continue
        # a real extraction MUST select the records -- a query with no select_all/select
        # can't extract anything (it would wrap to `reference(url).resolve()` with nothing
        # after). Reject it before it can look like a 0-row "success".
        ops = {s.name for s in expr._plan.steps if s.kind == "get"}
        if not ({"select", "select_all"} & ops):
            log.info("    query has no selection -- retrying with feedback")
            ask = (
                prompt + "\n\nYour previous query had NO selection so it extracts nothing."
                " You MUST select the repeating record with .select_all(...), pull each"
                " field with .extract(col=...), and END with .project(). Re-write it."
            )
            continue
        tested, rows = _test_query(expr, doc) if doc.ok else (False, [])
        # VALIDATE the extraction actually pulled content, not just that it ran: keep only
        # rows with a non-empty field, and require every non-optional field to have come out
        # somewhere. A query that matched a container but whose field selectors match nothing
        # (a guess, or content that isn't in the HTML) is NOT a success.
        good = _populated_rows(rows)
        missing = _empty_required_fields(good, brief)
        exe = _executable_query(expr, candidate_url, resolve)  # self-contained + runnable
        art = QueryArtifact(
            blob=exe.to_blob(),
            describe=exe.explain(),
            plan=exe._plan.model_dump(mode="json"),
            tested=tested,
            row_count=len(good),
            sample=list(good[:5]),
            base_urls=bases,
        )
        if tested and good and not missing:
            return art  # rows with real, complete content -- accept it
        best = best or art  # keep the first rebuildable one as a fallback
        # ran but did not truly extract: diagnose WHY (wrong record selector / empty fields /
        # a required field never populated / content not in the HTML) and hand the model a
        # concrete, human-readable hint.
        hint = _content_hint(expr, rows, brief, doc)
        log.info("    query did not extract valid content -- retrying with feedback: %s", hint)
        ask = prompt + f"\n\nYour previous query was:\n{expr.explain()}\n\n{hint}"
    if best is None:  # every attempt failed to author a usable query -- say so loudly
        log.warning("    could not author any query in %d attempt(s)", retries + 1)
    return best


# --------------------------------------------------------------------------- #
# 8. review  (LLM meta-review: grade the run's choices)
# --------------------------------------------------------------------------- #


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
        passed=bool(data.get("pass", True)),  # absent -> don't block (default pass)
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
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        try:
            return datetime.date(int(m[1]), int(m[2]), int(m[3]))
        except ValueError:
            pass
    return None


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

    date_cols = [f.split(".")[0] for f in brief.fields
                 if any(w in f.lower() for w in ("date", "publish", "time", "year"))]
    if not date_cols:
        return "", False
    dates = sorted(
        {d for r in rows if isinstance(r, dict) for c in date_cols
         if isinstance(r.get(c), str) and (d := _parse_date(r[c])) is not None},
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
    skeleton = _clip(doc.skeleton(max_lines=_FULL_SKELETON), _MAX_SKELETON_CHARS, "skeleton",
                     kind=("json" if doc.kind == "json" else "html")) if (doc is not None and doc.ok) else "(unavailable)"
    sample = json.dumps(list(q.sample)[:8], default=str, indent=2)
    tnote, stale = _timeliness(list(q.sample), brief)  # is the LATEST data present, per cadence?
    data = _ask_json(llm, render_prompt(
        "review_query",
        description=brief.description, fields_line=_fields_line(brief),
        query=q.describe, row_count=str(q.row_count), tested=str(q.tested),
        sample=_clip(sample, _MAX_LISTING_CHARS, "sample rows"),
        timeliness=tnote or "(no date field to assess timeliness)",
        # completeness is KEPT but DISABLED by default -- flip _CHECK_COMPLETENESS to gate on it
        completeness=(_COMPLETENESS_BLOCK if _CHECK_COMPLETENESS else _COMPLETENESS_OFF),
        skeleton=skeleton,
    ))
    review = _review_from_json("query", data)
    if stale:  # ENSURE the timeliness gate deterministically, whatever the model said
        review = review or Review(stage="query", verdict="poor")
        review.passed = False
        review.verdict = review.verdict or "poor"
        if tnote and tnote not in review.issues:
            review.issues = [tnote, *review.issues][:10]
        if not review.summary:
            review.summary = "the most recent data is missing (fails timeliness)"
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


def _gate(result: OnboardingResult, review: "Review | None") -> bool:
    """Record a stage review and GATE the pipeline on it: append it to ``result.reviews``
    and return whether the run may CONTINUE. A review that did not pass fails the run here
    (``ok=False`` + a reason naming the stage), so the review is integral -- a bad crawl /
    selection / query stops the pipeline, it is not graded after the fact. ``None`` (the
    review was skipped or the model gave nothing) does not gate."""
    if review is None:
        return True
    result.reviews.append(review)
    _trace(result, "%s review: %s%s", review.stage,
           "passed" if review.passed else "FAILED",
           f" — {review.summary}" if review.summary else "")
    if not review.passed:
        result.ok = False
        result.reason = f"{review.stage} review failed" + (f": {review.summary}" if review.summary else "")
        return False
    return True


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
    # GATE: the crawl review is an integral stage -- a crawl that didn't reach the data
    # fails the run here rather than pressing on to select a source that isn't there.
    if review and not _gate(result, review_crawl(artifacts, brief, llm=llm)):
        return result
    candidates = select_candidates(crawl, brief, llm=llm)
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
    # GATE: the selection review -- were the right URLs picked and the best source chosen?
    if review and not _gate(result, review_select(result, artifacts, brief, llm=llm)):
        return result
    # -- the flag-driven decision cascade for the chosen source, in order ----------
    # (1) reference: the data API if the SPA is backed by one, else the page URL.
    result.reference = write_reference(evaluation, wc=wc)
    query_url = evaluation.api_endpoint or evaluation.url
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
    # (4) query: authored from the skeleton, told to page when the source paginates.
    result.query = write_query(
        query_url, brief, wc=wc, llm=llm, browser=_mode(browser),
        paginated=evaluation.has_pagination, resolve=result.resolve,
    )
    if isinstance(llm, LlmClient):
        result.cost_usd = llm.spent_usd
    # a real success EXTRACTS data: a query that ran but produced 0 rows is not ok
    # (it selected nothing / didn't project / hit the wrong source).
    q = result.query
    result.ok = q is not None and q.row_count > 0
    if result.ok:
        result.reason = ""
    elif q is not None:
        result.reason = "authored query extracted 0 rows"
    else:
        result.reason = "could not author a query"
    if q is not None:
        note("query authored (tested=%s, %d row[s])", q.tested, q.row_count)
    # GATE: the query review -- does the output match the brief, are the selectors right,
    # is it complete? A failing review fails the run even if the query technically ran.
    if review and result.ok:
        _gate(result, review_query(result, artifacts, brief, llm=llm))
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
