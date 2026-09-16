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
from typing import Any, Callable, Literal, Sequence

from pydantic import BaseModel

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
from ..surfaces import Reference, WebClient
from .llm import Budget, BudgetExceeded, LlmClient
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


# --------------------------------------------------------------------------- #
# Artifacts (the typed things that flow between stages).
# --------------------------------------------------------------------------- #


def _parse_frontmatter(text: str) -> "tuple[dict[str, Any], str]":
    """A tiny YAML-frontmatter reader (no dependency): a leading ``---`` block of
    ``key: value`` scalars and ``key:`` + indented ``- item`` lists, then the body.
    Returns ``(front, body)``; no frontmatter -> ``({}, text)``."""
    if not text.lstrip().startswith("---"):
        return {}, text
    rest = text.lstrip()[3:].lstrip("\n")
    end = rest.find("\n---")
    if end == -1:
        return {}, text
    block, body = rest[:end], rest[end + 4 :].lstrip("\n")
    front: dict[str, Any] = {}
    key: str | None = None
    for raw in block.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        if raw.lstrip().startswith("- ") and key is not None:  # a list item
            front.setdefault(key, [])
            if isinstance(front[key], list):
                front[key].append(raw.split("- ", 1)[1].strip().strip("'\""))
        elif ":" in raw and not raw.startswith(" "):
            k, _, v = raw.partition(":")
            key = k.strip()
            v = v.strip().strip("'\"")
            front[key] = v if v else []  # a value, or an empty list to be filled
    return front, body


def _coerce(v: str) -> Any:
    """A frontmatter scalar to its natural type: an int / float / bool where it reads
    as one, else the string."""
    low = v.strip().lower()
    if low in ("true", "false"):
        return low == "true"
    if low in ("null", "none", "~", ""):
        return None
    try:
        return int(v)
    except ValueError:
        pass
    try:
        return float(v)
    except ValueError:
        return v.strip()


def _kv_items(items: "list[str]") -> "dict[str, Any]":
    """Parse ``["max_pages: 30", "browser: false"]`` -> ``{"max_pages": 30, "browser":
    False}`` -- the ``key: value`` list items a frontmatter block carries."""
    out: dict[str, Any] = {}
    for item in items:
        key, sep, value = item.partition(":")
        if sep:
            out[key.strip()] = _coerce(value)
    return out


class SchemaField(BaseModel):
    """One field in the target schema: a ``name``, an optional ``description`` (what it
    is / how to fill it), and nested ``children`` for structured values (a ``price``
    with ``value`` / ``unit`` / ``modifiers``)."""

    name: str
    description: str = ""
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
    name: str = ""  # a short slug id (e.g. "product-catalogue")
    title: str = ""  # a human title
    look: list[str] = []  # natural-language guides: what kinds of pages to head for
    ignore: list[str] = []  # natural-language guides: what kinds of pages to skip
    crawl: dict[str, Any] = {}  # pipeline crawl overrides (max_pages/depth/rounds/browser)

    @classmethod
    def from_markdown(cls, text: str) -> "Brief":
        """Build a :class:`Brief` from a markdown document. Frontmatter keys: ``name`` /
        ``title``; ``schema`` (a list of ``path: description`` items -- dotted paths
        nest, the text is that field's description); ``look`` / ``ignore`` (NL guide
        lines); ``crawl`` (a list of ``key: value`` pipeline crawl overrides);
        ``description`` (else the body). Extra list items without a ``:`` are treated as
        bare field names."""
        front, body = _parse_frontmatter(text)

        def as_list(v: Any) -> list[str]:
            return [str(x) for x in v] if isinstance(v, list) else ([str(v)] if v else [])

        fields: list[str] = []
        descriptions: dict[str, str] = {}
        for item in as_list(front.get("schema") or front.get("fields")):
            path, sep, desc = item.partition(":")
            path = path.strip()
            if path:
                fields.append(path)
                if sep and desc.strip():
                    descriptions[path] = desc.strip()

        crawl = front.get("crawl")
        return cls(
            description=str(front.get("description") or body).strip(),
            fields=fields,
            descriptions=descriptions,
            name=str(front.get("name") or ""),
            title=str(front.get("title") or ""),
            look=as_list(front.get("look")),
            ignore=as_list(front.get("ignore")),
            crawl=_kv_items(crawl) if isinstance(crawl, list) else {},
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
        for path in self.fields:
            parent = ""
            for part in (p.strip() for p in path.split(".") if p.strip()):
                full = f"{parent}.{part}" if parent else part
                node = index.get(full)
                if node is None:
                    node = SchemaField(name=part, description=self.descriptions.get(full, ""))
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
    scrapability: int = 0  # 0-10; higher is easier/cleaner to scrape
    verdict: str = ""  # the model's one-line judgement
    #: the detected flags on the page (name -> confidence), and, when the SPA is
    #: backed by a same-origin data API, the endpoint to query INSTEAD of scraping.
    flags: dict[str, float] = {}
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
    sample: list[str] = []  # up to 3 produced rows (stringified), for a sanity check
    #: the source URLs this one query runs against, unioned. Usually one, but a dataset
    #: split across distinct URLs (e.g. /products/cloud + /products/onprem -- NOT
    #: pagination) lists them all; :func:`run_query` resolves the query per base and
    #: concatenates the rows.
    base_urls: list[str] = []


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
    cost_usd: float = 0.0  # LLM spend for this company (when an LlmClient was used)


def _trace(result: OnboardingResult, message: str, *args: Any) -> None:
    """Log one pipeline step at INFO and append it to the result's ``steps`` trace, so a
    run is observable live (logging) and after the fact (``result.steps``)."""
    rendered = message % args if args else message
    result.steps.append(rendered)
    log.info("%s: %s", result.company, rendered)


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


def _ask_json(llm: LLM, prompt: str) -> Any:
    """Run ``llm`` and parse a JSON value from its reply; ``None`` on unparseable."""
    try:
        return json.loads(_json_blob(llm(prompt)))
    except (json.JSONDecodeError, ValueError):
        return None


def _schema_outline(fields: "list[SchemaField]", indent: int = 0) -> str:
    """A :class:`SchemaField` tree rendered as an indented outline for a prompt --
    ``- name — description`` per field, nested children indented under their parent."""
    lines: list[str] = []
    for f in fields:
        desc = f" — {f.description}" if f.description else ""
        lines.append("  " * indent + f"- {f.name}{desc}")
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
    "spa", "anti_bot_triggered", "login_required", "pagination", "forms", "buttons",
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


def search_web(
    brief: Brief,
    company: str,
    *,
    search: SearchFn,
    k: int = 6,
    llm: LLM | None = None,
) -> list[Seed]:
    """Seed URLs for ``company`` + ``brief``. The query is ``"<company> <brief>"`` by
    default; pass ``llm`` to have the model craft a sharper search query first."""
    query = f"{company} {brief.description}".strip()
    if llm is not None:
        crafted = llm(
            render_prompt(
                "search_query",
                company=company,
                description=brief.description,
                fields_line=_fields_line(brief),
            )
        ).strip().splitlines()
        if crafted and crafted[0].strip():
            query = crafted[0].strip()
    return [
        Seed(url=h.url, title=h.title, why=h.snippet)
        for h in search(query, k)
        if h.url
    ]


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


def _filter_frontier(edges: Sequence[Any], brief: Brief) -> list[Any]:
    """Prune the frontier before the model spends a pick on it: collapse paginated URL
    sets and repeated similar-API calls to one representative each (keeping the first --
    the frontier is already best-first). This is a STRUCTURAL de-dup only; ``look`` /
    ``ignore`` are natural-language guides the model applies when it picks, not literal
    URL filters. Keeps the crawl from wasting budget on many versions of one thing."""
    kept: list[Any] = []
    seen: set[tuple[str, str, frozenset[str]]] = set()
    for e in edges:
        key = _frontier_key(e.url)
        if key in seen:
            continue
        seen.add(key)
        kept.append(e)
    return kept


def _pick_edges(llm: LLM, brief: Brief, frontier: Sequence[Any]) -> list[str]:
    """Ask the model which frontier edges to expand next -- the ones most likely to
    reach the dataset, preferring a queryable source (an API over the whole dataset)
    to a page that lists only part of it, and following pagination when it must."""
    listing = "\n".join(
        f"{i}. {e.url}   (link text: {e.text!r})" for i, e in enumerate(frontier)
    )
    picked = _ask_json(
        llm,
        render_prompt(
            "pick_edges",
            description=brief.description,
            fields_line=_fields_line(brief),
            listing=listing,
        ),
    )
    if not isinstance(picked, list):
        return []
    urls: list[str] = []
    for idx in picked:
        if isinstance(idx, int) and 0 <= idx < len(frontier):
            urls.append(frontier[idx].url)
    return urls


def crawl_from_seeds(
    seeds: Sequence[Seed],
    brief: Brief,
    *,
    wc: WebClient,
    llm: LLM,
    max_pages: int = 20,
    rounds: int = 4,
    browser: bool = True,
) -> Any:
    """A hand-driven crawl steered by the model: fetch the seeds, then each round let
    the model pick which discovered edges to expand (favouring a queryable source),
    up to ``rounds`` rounds or ``max_pages`` pages. Returns the finished ``Crawl``
    (read ``.pages`` for the retained Documents). The brief's ``crawl`` block overrides
    ``max_pages`` / ``rounds`` / ``depth`` / ``browser`` per dataset."""
    cfg = brief.crawl
    max_pages = int(cfg.get("max_pages", max_pages))
    rounds = int(cfg.get("rounds", rounds))
    browser = bool(cfg.get("browser", browser))
    depth = int(cfg.get("depth", 3))
    seed_urls = [s.url for s in seeds if s.url]
    crawl = wc.crawl(
        seed_urls, auto=False, browser=browser, max_pages=max_pages, depth=depth,
        obey_robots=False,
    )
    crawl.step(seed_urls)  # round 0: fetch the seeds, discover their edges
    for _ in range(rounds):
        if not crawl.frontier or len(crawl.pages) >= max_pages:
            break
        # collapse paginated/similar-API duplicates before the model spends a pick
        candidates = _filter_frontier(list(crawl.frontier), brief)
        picks = _pick_edges(llm, brief, candidates)
        if not picks:
            break
        crawl.step(picks)
    return crawl


# --------------------------------------------------------------------------- #
# 3. select_candidates
# --------------------------------------------------------------------------- #


def select_candidates(crawl: Any, brief: Brief, *, llm: LLM) -> list[Candidate]:
    """Rank the crawled pages into must / should / could-evaluate candidates by
    scrapability + likely relevance to the dataset."""
    # crawl.pages are lean PageCards by default (url / title / flags already projected)
    pages = [{"url": p.final_url or p.url, "title": p.title, "flags": p.flags} for p in crawl.pages]
    if not pages:
        return []
    rows = _ask_json(
        llm,
        render_prompt(
            "select_candidates",
            description=brief.description,
            fields_line=_fields_line(brief),
            pages_json=json.dumps(pages, indent=0),
        ),
    )
    out: list[Candidate] = []
    for r in rows if isinstance(rows, list) else []:
        if isinstance(r, dict) and r.get("url"):
            out.append(Candidate.model_validate({**r, "url": str(r["url"])}))
    _rank = {"must": 0, "should": 1, "could": 2}
    out.sort(key=lambda c: _rank.get(c.tier, 3))
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
    # a login wall blocks the dataset -- no query reaches it; drop the candidate early.
    if flags["login_required"].present:
        return CandidateEval(url=candidate.url, verdict="login required", flags=flag_map)
    skeleton = doc.skeleton(max_lines=70)
    endpoints = [c.url for c in doc.xhr_endpoints()]
    spa = flags["spa"]
    # if a SPA is backed by same-origin XHR endpoints, the API is the real source --
    # querying it beats scraping the rendered page. Record the first as a candidate.
    api_endpoint = spa.value[0] if spa.present and isinstance(spa.value, list) and spa.value else None
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
    data.update(url=candidate.url, flags=flag_map, api_endpoint=api_endpoint, interactive=interactive)
    return CandidateEval.model_validate(data)


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
    spa = "spa" in by
    triggered = by.get("anti_bot_triggered")
    stealth = bool(triggered and triggered.remedy == "stealth")
    proxy = bool(triggered and triggered.remedy in ("proxy", "stealth"))
    return Resolve(
        browser=BrowserPolicy(when="always") if (spa or stealth) else None,
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


def _test_query(expr: Any, doc: Any) -> "tuple[bool, list[Any]]":
    """Run the authored query against the fetched source ``doc`` to prove it loads and
    extracts. The query is the document-level extraction (``wq.doc...``), so it collects
    directly against the resolved document. Returns ``(ran_without_error, rows)`` -- a
    query that raises is not ``tested`` and its rows are empty."""
    try:
        result = expr.collect(doc)
    except Exception:  # noqa: BLE001 - a query that can't run against the source
        return False, []
    if result is None:
        return True, []
    try:
        rows = list(result)
    except TypeError:  # a scalar/Field result, not a row set
        rows = [result]
    return True, rows


def run_query(
    artifact: QueryArtifact, *, wc: WebClient, browser: BrowserMode = "auto"
) -> list[Any]:
    """Run an authored query against ALL its ``base_urls`` and concatenate the rows --
    so a dataset split across distinct URLs (``/products/cloud`` + ``/products/onprem``)
    comes back as one list. The query is the document-level extraction; the pipeline
    fetches each base (the caller owns fetch/resolve) and collects the query against the
    resolved document."""
    expr = from_blob(artifact.blob)
    out: list[Any] = []
    for url in artifact.base_urls or []:
        doc = wc.fetch(url, browser=browser, optional=True)
        if not doc.ok:
            continue
        try:
            result = expr.collect(doc)
        except Exception:  # noqa: BLE001 - a base whose query fails contributes nothing
            continue
        out.extend(list(result) if result is not None else [])
    return out


def write_query(
    candidate_url: str,
    brief: Brief,
    *,
    wc: WebClient,
    llm: LLM,
    browser: BrowserMode = "auto",
    paginated: bool = False,
    retries: int = 1,
    extra_urls: Sequence[str] = (),
) -> QueryArtifact | None:
    """Have the model author a lazy query for the dataset from the page skeleton, then
    reload it (``from_blob``) AND run it against the source to confirm it extracts rows.
    Retries on an invalid or empty query, preferring one that actually produces rows;
    returns the best :class:`QueryArtifact` (with its plan + a tested row sample), or
    ``None`` if none rebuilt. ``paginated`` tells the author to also capture the
    next-page link. ``extra_urls`` are further base URLs the SAME query also runs
    against (a dataset spread across distinct URLs) -- recorded on ``base_urls`` for
    :func:`run_query` to union."""
    doc = wc.fetch(candidate_url, browser=browser, optional=True)
    skeleton = doc.skeleton(max_lines=90) if doc.ok else ""
    prompt = _query_prompt(brief, skeleton, paginated=paginated)
    bases = [candidate_url, *extra_urls]
    best: QueryArtifact | None = None
    for _ in range(retries + 1):
        blob = _json_blob(llm(prompt))
        try:
            expr = from_blob(blob)
        except Exception:  # noqa: BLE001 - any malformed blob -> retry / give up
            continue
        tested, rows = _test_query(expr, doc) if doc.ok else (False, [])
        art = QueryArtifact(
            blob=blob,
            describe=expr.explain(),
            plan=expr._plan.model_dump(mode="json"),
            tested=tested,
            row_count=len(rows),
            sample=[str(r)[:200] for r in rows[:3]],
            base_urls=bases,
        )
        if tested and rows:
            return art  # a query that actually extracts rows -- accept it
        best = best or art  # keep the first rebuildable one as a fallback
    return best


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
) -> OnboardingResult:
    """Run the whole pipeline for one company: search -> crawl -> select -> evaluate
    -> write the reference, resolve, and query for the best source found.

    Pass a :class:`~webclient.pipelines.llm.Budget` to cap LLM spend for this run: when
    an :class:`~webclient.pipelines.llm.LlmClient` is the injected ``llm`` the budget is
    attached to it, and if the cap is hit mid-pipeline the run stops and reports
    ``ok=False`` / ``reason="llm budget exceeded"`` instead of raising to the caller.
    """
    result = OnboardingResult(company=company, brief=brief)
    # Thread the cap into an LlmClient so its per-call spend is enforced. A plain
    # callable llm (e.g. a test stub) carries no cost, so there is nothing to cap.
    if budget is not None and isinstance(llm, LlmClient):
        llm.budget = budget
    try:
        return _onboard_company(
            company, brief, result,
            wc=wc, llm=llm, search=search, max_pages=max_pages, browser=browser,
        )
    except BudgetExceeded:
        result.ok = False
        result.reason = "llm budget exceeded"
        return result


def _onboard_company(
    company: str,
    brief: Brief,
    result: OnboardingResult,
    *,
    wc: WebClient,
    llm: LLM,
    search: SearchFn,
    max_pages: int,
    browser: bool,
) -> OnboardingResult:
    _trace(result, "searching the web for seeds")
    seeds = search_web(brief, company, search=search, llm=llm)
    if not seeds:
        result.reason = "no search seeds"
        return result
    _trace(result, "%d seed(s); crawling for the dataset", len(seeds))
    crawl = crawl_from_seeds(
        seeds, brief, wc=wc, llm=llm, max_pages=max_pages, browser=browser
    )
    _trace(result, "crawled %d page(s), %d failed", len(crawl.pages), len(crawl.failures))
    candidates = select_candidates(crawl, brief, llm=llm)
    if not candidates:
        result.reason = "no candidate pages"
        return result
    _trace(result, "%d candidate(s); evaluating best-first", len(candidates))
    evaluation = evaluate_candidates(
        candidates, brief, wc=wc, llm=llm, browser=_mode(browser)
    )
    if evaluation is None or not evaluation.dataset_present:
        result.reason = "no usable source found"
        result.evaluation = evaluation
        return result
    result.evaluation = evaluation
    _trace(
        result, "chose %s (queryable=%s, scrapability=%d)",
        evaluation.url, evaluation.is_queryable, evaluation.scrapability,
    )
    # -- the flag-driven decision cascade for the chosen source, in order ----------
    # (1) reference: the data API if the SPA is backed by one, else the page URL.
    result.reference = write_reference(evaluation, wc=wc)
    query_url = evaluation.api_endpoint or evaluation.url
    doc = wc.fetch(query_url, browser=_mode(browser), optional=True)
    flags = _read_flags(doc) if doc.ok else {}
    # (2) a login wall on the source itself -> no query reaches the data; stop.
    if flags.get("login_required") is not None and flags["login_required"].present:
        result.reason = "the source requires login"
        return result
    # (3) resolve policy: spa -> browser, anti_bot_triggered -> proxy/stealth.
    result.resolve = write_resolve(list(flags.values()))
    fired = [n for n, f in flags.items() if f.present]
    _trace(result, "flags fired: %s; authoring the query", ", ".join(fired) or "none")
    # (4) query: authored from the skeleton, told to page when the source paginates.
    result.query = write_query(
        query_url, brief, wc=wc, llm=llm, browser=_mode(browser),
        paginated=evaluation.has_pagination,
    )
    if isinstance(llm, LlmClient):
        result.cost_usd = llm.spent_usd
    result.ok = result.query is not None
    result.reason = "" if result.ok else "could not author a query"
    if result.query is not None:
        _trace(
            result, "query authored (tested=%s, %d row[s]); spent $%.4f",
            result.query.tested, result.query.row_count, result.cost_usd,
        )
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
) -> list[OnboardingResult]:
    """Onboard several companies for the same brief (sequentially, one crawl each).

    A shared ``budget`` caps LLM spend across the WHOLE run: once it is exhausted the
    remaining companies report ``ok=False`` / ``reason="llm budget exceeded"``."""
    return [
        onboard_company(
            c, brief, wc=wc, llm=llm, search=search, max_pages=max_pages,
            browser=browser, budget=budget,
        )
        for c in companies
    ]
