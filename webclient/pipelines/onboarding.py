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
from typing import Any, Callable, Literal, Sequence

from pydantic import BaseModel

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


class Brief(BaseModel):
    """What dataset we want to onboard. ``description`` is the free-text ask (e.g.
    "the company's product catalogue"); ``fields`` are the columns each record
    should carry, when known (they steer the query author)."""

    description: str
    fields: list[str] = []


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
    blob: str  # the portable lazy-query blob (rebuildable with from_blob)
    describe: str  # a readable one-line rendering of the chain


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


def _fields_line(brief: Brief) -> str:
    return f" Target fields: {', '.join(brief.fields)}." if brief.fields else ""


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
            "Write a single web-search query (no quotes, no prose) that would find "
            f"the following for the company '{company}': {brief.description}."
            f"{_fields_line(brief)}"
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


def _pick_edges(llm: LLM, brief: Brief, frontier: Sequence[Any]) -> list[str]:
    """Ask the model which frontier edges to expand next -- the ones most likely to
    reach the dataset, preferring a queryable source (an API over the whole dataset)
    to a page that lists only part of it, and following pagination when it must."""
    listing = "\n".join(
        f"{i}. {e.url}   (link text: {e.text!r})" for i, e in enumerate(frontier)
    )
    picked = _ask_json(
        llm,
        "You are crawling a company site to reach this dataset: "
        f"{brief.description}.{_fields_line(brief)}\n"
        "From the frontier links below, choose the ones worth fetching next. Prefer "
        "links that lead to a single queryable source of the WHOLE dataset (an API, a "
        "data/export endpoint, a full listing) over a page that shows only a slice. "
        "Follow pagination ('next', page N) when the dataset spans pages. Ignore "
        "nav chrome, legal, and social links.\n\n"
        f"{listing}\n\n"
        'Reply with only a JSON array of the chosen link numbers, e.g. [0, 3, 4].',
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
    (read ``.pages`` for the retained Documents)."""
    seed_urls = [s.url for s in seeds if s.url]
    crawl = wc.crawl(
        seed_urls, auto=False, browser=browser, max_pages=max_pages, obey_robots=False
    )
    crawl.step(seed_urls)  # round 0: fetch the seeds, discover their edges
    for _ in range(rounds):
        if not crawl.frontier or len(crawl.pages) >= max_pages:
            break
        picks = _pick_edges(llm, brief, list(crawl.frontier))
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
    pages = []
    for p in crawl.pages:
        pages.append(
            {
                "url": p.final_url or p.url,
                "title": p.title,
                "flags": [f.name for f in p.flags()],
            }
        )
    if not pages:
        return []
    rows = _ask_json(
        llm,
        f"We want to scrape this dataset: {brief.description}.{_fields_line(brief)}\n"
        "Here are the crawled pages (with detected flags like 'spa' = JS-rendered, "
        "'pagination' = spans pages, 'login_required' = gated):\n"
        f"{json.dumps(pages, indent=0)}\n\n"
        "Pick the pages worth evaluating as the source to scrape. For each, give its "
        '"url", a "kind" ("api" | "page" | "spa"), a "tier" ("must" | "should" | '
        '"could") by how likely+scrapeable it is, and a short "note". '
        "Reply with only a JSON array of such objects.",
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
        f"Dataset wanted: {brief.description}.{_fields_line(brief)}\n"
        f"Candidate URL: {candidate.url}\n"
        f"Detected flags (name: confidence): {json.dumps(flag_map)}\n"
        f"Observed data endpoints (XHR/fetch): {json.dumps(endpoints)}\n"
        f"Page skeleton:\n{skeleton}\n\n"
        "Assess this page as the source to scrape and reply with only a JSON object "
        'with keys: "dataset_present" (bool), "is_queryable" (bool: is there an API/'
        'endpoint serving the WHOLE dataset?), "sort_order" (str|null), '
        '"completeness" ("full"|"partial"|"unknown"), "has_pagination" (bool), '
        '"has_filters" (bool), "dataset_is_subset" (bool: is our ask a subset of '
        'what is here?), "mostly_unstructured" (bool), "drilldown_links" (bool), '
        '"scrapability" (int 0-10), "verdict" (one short sentence).',
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
    # about fetching, resolving, or running -- only CSS selectors and the query DSL.
    pager = (
        "\nThe dataset spans multiple pages: also extract the next-page link "
        '(a rel="next" anchor) as a field named "next" so the caller can follow it.'
        if paginated else ""
    )
    return (
        f"{lazy_query_guide()}\n\n"
        "----\n"
        "Using ONLY the query DSL above, write a lazy web query that extracts this "
        f"dataset from the page: {brief.description}.{_fields_line(brief)}{pager}\n"
        "Base your CSS selectors on this page skeleton:\n"
        f"{skeleton}\n\n"
        "Author the query rooted at wq.ref.resolve(), select the repeating records, "
        "extract the target fields, and end with .project(). Reply with ONLY the "
        "query's portable blob from expr.to_blob() -- a single JSON object, nothing else."
    )


def write_query(
    candidate_url: str,
    brief: Brief,
    *,
    wc: WebClient,
    llm: LLM,
    browser: BrowserMode = "auto",
    paginated: bool = False,
    retries: int = 1,
) -> QueryArtifact | None:
    """Have the model author a lazy query for the dataset from the page skeleton, then
    validate it by rebuilding it with :func:`from_blob`. Retries once on an invalid
    blob. Returns ``None`` if no valid query could be authored. ``paginated`` (from the
    pagination flag) tells the author to also capture the next-page link."""
    doc = wc.fetch(candidate_url, browser=browser, optional=True)
    skeleton = doc.skeleton(max_lines=90) if doc.ok else ""
    prompt = _query_prompt(brief, skeleton, paginated=paginated)
    for _ in range(retries + 1):
        blob = _json_blob(llm(prompt))
        try:
            expr = from_blob(blob)
        except Exception:  # noqa: BLE001 - any malformed blob -> retry / give up
            continue
        return QueryArtifact(blob=blob, describe=expr.explain())
    return None


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
) -> OnboardingResult:
    """Run the whole pipeline for one company: search -> crawl -> select -> evaluate
    -> write the reference, resolve, and query for the best source found."""
    result = OnboardingResult(company=company, brief=brief)
    seeds = search_web(brief, company, search=search, llm=llm)
    if not seeds:
        result.reason = "no search seeds"
        return result
    crawl = crawl_from_seeds(
        seeds, brief, wc=wc, llm=llm, max_pages=max_pages, browser=browser
    )
    candidates = select_candidates(crawl, brief, llm=llm)
    if not candidates:
        result.reason = "no candidate pages"
        return result
    evaluation = evaluate_candidates(
        candidates, brief, wc=wc, llm=llm, browser=_mode(browser)
    )
    if evaluation is None or not evaluation.dataset_present:
        result.reason = "no usable source found"
        result.evaluation = evaluation
        return result
    result.evaluation = evaluation
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
    # (4) query: authored from the skeleton, told to page when the source paginates.
    result.query = write_query(
        query_url, brief, wc=wc, llm=llm, browser=_mode(browser),
        paginated=evaluation.has_pagination,
    )
    result.ok = result.query is not None
    result.reason = "" if result.ok else "could not author a query"
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
) -> list[OnboardingResult]:
    """Onboard several companies for the same brief (sequentially, one crawl each)."""
    return [
        onboard_company(
            c, brief, wc=wc, llm=llm, search=search, max_pages=max_pages, browser=browser
        )
        for c in companies
    ]
