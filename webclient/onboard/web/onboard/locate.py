"""Locate -- ``goal -> Reference``: find WHERE the dataset lives and hand Author the hints.

The phases (each skippable by supplying its output on the brief):
  1. **search**  -- turn the goal into seed URLs (an injected :class:`Search`; skipped if
     ``seeds`` given). No web-search transport lives in these packages, so search is pluggable.
  2. **crawl**   -- reach candidate pages from the seeds (:mod:`web.crawl`; skipped if
     ``candidates`` given).
  3. **evaluate**-- score every candidate with the WHOLE detection surface: :func:`web.resolve.flags`
     (noisy-OR conclusions over :mod:`web.resolve.signals`) + :meth:`Document.records`.
  4. **select**  -- the best-scoring candidate that actually holds the dataset.
  5. **prefer API** -- if a same-origin JSON data-API backs the page AND its data is consistent
     with the page, root the Reference at THAT endpoint (JSON is cleaner than scraping HTML).

Deterministic and LLM-free; a model, if any, plugs in only as the ``search`` callable.
"""

from __future__ import annotations

import re
from typing import Protocol, runtime_checkable
from urllib.parse import urlparse

from web.crawl import Crawler, FrontierMiddleware, Goal
from web.fetch import ClientPool, NetworkEvent, Request, Snapshot, WebException, emit
from web.parse import Document, parse
from web.resolve import Flag, Resolver, document, flags
from web.resolve import profiles as _rp

from .llm import Llm, ReasonEvent
from .models import DOWNLOAD_EXTENSIONS, LocateBrief, Reference

#: URL markers of API DOCUMENTATION / dev portals -- never a scrapable dataset, even if crawled.
_DOCS = (
    "/docs",
    "/documentation",
    "/developer",
    "/developers",
    "/api-docs",
    "/swagger",
    "/redoc",
    "/reference/",
    "/help/",
    "/support/",
)
#: pager remedy strings, in the order they win (a real pager beats scroll beats a cursor guess).
_PAGERS = (("paginated", "paginate"), ("infinite_scroll", "paginate:scroll"))


@runtime_checkable
class Search(Protocol):
    """Turn a goal into seed URLs. Injected (an LLM / a search API / a fixed list) so Locate stays
    transport-agnostic and offline-testable; everything above depends on this, not a vendor.
    """

    async def __call__(self, goal: str) -> list[str]: ...


def _is_docs(url: str) -> bool:
    low = url.lower()
    return any(m in low for m in _DOCS)


def _present(doc: Document, by: "dict[str, Flag]") -> bool:
    """Whether the dataset is plausibly ON this page: a JSON doc IS data, else a repeating record
    region / structured data / a data island must be present."""
    if doc.kind == "json":
        return True
    return bool(doc.records(top_k=1)) or "structured_data" in by or "data_api" in by


def _score(doc: Document, by: "dict[str, Flag]") -> float:
    """A deterministic dataset-likeness score (see :mod:`.evaluate` learnings): dataset-present x
    scrapability, minus gates that block extraction. Higher is a better source. A hard GATE (a login
    wall, API docs, or an anti-bot block) disqualifies the page outright -- its "records" are the
    wall/challenge, not the dataset, so it must never outrank a clean source no matter how table-like
    it looks."""
    if "auth_required" in by:  # a login wall -- no query reaches the dataset
        return -1.0
    # any tier of anti-bot wall (ANTI-BOT.md §4) is the challenge/block page, not the dataset -- its
    # "records" are the wall, so it must never outrank a clean source however table-like it looks.
    if any(f in by for f in ("js_challenge", "captcha", "ip_blocked", "rate_limited")):
        return -1.0
    if _is_docs(doc.url):  # API docs are never the data source
        return -1.0
    if not _present(doc, by):
        return 0.0
    if doc.kind == "json":  # a served JSON/feed document is the dataset itself
        return 9.0
    regions = doc.records(top_k=1)
    # cap the record-region contribution so a huge nav/boilerplate table cannot dominate a real
    # (smaller) dataset -- record PRESENCE matters more than raw region size.
    base = 4.0 + (min(regions[0].score, 40.0) * 0.1 if regions else 0.0)
    if "structured_data" in by:
        base += 2.0  # machine-readable data about itself -- cleaner to extract
    if "data_api" in by:
        base += 1.0
    if "empty" in by:
        base -= 3.0
    return base


def _record_selector(doc: Document) -> "str | None":
    regions = doc.records(top_k=1)
    return regions[0].item_selector if regions else None


def _pagination(by: "dict[str, Flag]") -> "str | None":
    for name, remedy in _PAGERS:
        if name in by:
            return remedy
    return None


def data_api_endpoints(doc: Document) -> list[str]:
    """Same-origin JSON/data-API URLs the page points at: declared JSON/feed ``<link>``s, then any
    ``/api/``- or ``.json``-looking link. Deterministic (the DOM only, no live-XHR capture needed),
    ordered most-declared first, deduped."""
    host = urlparse(doc.url).hostname
    out: list[str] = []
    meta = doc.metadata()
    declared = list(meta.feeds)
    for el in doc.select_all("link[rel=alternate][type*=json], link[type*=json]"):
        href = el.attr("href")
        if href:
            declared.append(href)
    linky = [
        u for u in doc.links() if "/api/" in u.lower() or u.lower().split("?")[0].endswith(".json")
    ]
    for u in [*declared, *linky]:
        if urlparse(u).hostname == host and u not in out:
            out.append(u)
    return out


def _consistent(page: Document, api: Document) -> bool:
    """Whether ``api`` (a JSON response) BACKS ``page``: enough of its distinctive scalar leaves
    appear in the page's text. Guards against preferring an unrelated feed / a stale endpoint.
    """
    if api.kind != "json":
        return False
    leaves = [v for v in api.json_leaves(budget=2000) if len(v) >= 3][:40]
    if not leaves:
        return False
    text = page.text.lower()
    hits = sum(1 for v in leaves if v.lower() in text)
    return hits >= max(2, len(leaves) // 4)


def _json_xhr(snap: Snapshot) -> "list[tuple[str, Document]]":
    """The same-origin XHR/``fetch`` responses in a captured Snapshot whose body is JSON -- the live
    data-API behind the page, read from a render's network stream (not a URL guessed from the DOM).
    Bodies are already drained onto the Snapshot, so there is no re-fetch. Empty unless the Snapshot
    came from a browser profile (an HTTP-only fetch emits no network events)."""
    host = urlparse(snap.request.url).hostname
    out: "list[tuple[str, Document]]" = []
    seen: set[str] = set()
    for ev in snap.events:
        if not isinstance(ev, NetworkEvent) or ev.resource_type not in ("xhr", "fetch"):
            continue
        if not ev.body or ev.url in seen or urlparse(ev.url).hostname != host:
            continue
        seen.add(ev.url)
        doc = parse(ev.body, url=ev.url)
        if doc.kind == "json":
            out.append((ev.url, doc))
    return out


async def _observed(page: Document, resolver: Resolver) -> "list[tuple[str, Document]]":
    """The page's JSON XHR/``fetch`` stream, captured by rendering through the CALLER's resolver
    (a browser profile) -- see :func:`_json_xhr`. Locate never launches a browser on its own here.
    """
    return _json_xhr(await resolver.snapshot(Request(url=page.url)))


async def _prefer_api(page: Document, ref: Reference, resolver: Resolver) -> Reference:
    """Apply the XHR-preference rule: root the Reference at a same-origin JSON data-API that is
    consistent with the page, instead of the page. Never a blind/hallucinated pick -- the endpoint's
    data is checked against the page first. Two passes: the cheap DOM-declared/link endpoints
    (resolved), then -- only when the page is JS-gated (``needs_browser``, so its data is loaded by
    script, not in the static DOM) -- the real network stream it fires in a browser (:func:`_observed`,
    bodies already captured). The first consistent JSON source wins. A plain static page never
    launches a browser here."""
    for url in data_api_endpoints(page):
        api = await resolver.resolve(Request(url=url))
        if _consistent(page, api):
            return ref.model_copy(update={"url": url, "kind": api.kind, "api_endpoint": url})
    if ref.needs_browser:  # a JS-gated page: mine the XHR/fetch stream for the API that feeds it
        for url, api in await _observed(page, resolver):
            if _consistent(page, api):
                return ref.model_copy(update={"url": url, "kind": api.kind, "api_endpoint": url})
    return ref


def _reference(doc: Document, by: "dict[str, Flag]") -> Reference:
    """Build the Reference for a chosen candidate from its flags + record detection (pre-API). The
    working ``profile`` is stamped later by :func:`_working_profile` (the tier that actually fetched
    it), not guessed from the (over-firing) ``needs_browser`` heuristic."""
    render = {"needs_browser", "spa", "iframe"}
    return Reference(
        url=doc.url,
        kind=doc.kind,
        page_url=doc.url,
        flags=sorted(by),
        signals=sorted({s.name for f in by.values() for s in f.signals}),
        assessment=sorted(by.values(), key=lambda f: -f.confidence),  # full report: why each fired
        record_selector=_record_selector(doc),
        pagination=_pagination(by),
        needs_browser=any(n in by for n in render),
        detail={"score": round(_score(doc, by), 3), "reason": _reason(doc, by)},
    )


def _reason(doc: Document, by: "dict[str, Flag]") -> str:
    """A human WHY this candidate was chosen -- what made it look like the dataset."""
    bits: list[str] = []
    if doc.kind == "json":
        bits.append("a JSON data document (the dataset itself)")
    elif "record_list" in by:
        bits.append(f"a repeating record region ({_record_selector(doc)})")
    if "structured_data" in by:
        bits.append("machine-readable structured data (JSON-LD/microdata)")
    if "data_api" in by:
        bits.append("a JSON data-API backs the page")
    if "paginated" in by:
        bits.append("paginated (the pipeline follows the pager)")
    return "; ".join(bits) or "the highest dataset-likeness score among the candidates"


def _download_targets(doc: Document) -> "list[str]":
    """The downloadable-file links on the page (by extension) -- the 'records' of a download brief."""
    return [u for u in doc.links() if u.lower().split("?")[0].endswith(DOWNLOAD_EXTENSIONS)]


def _field_bonus(doc: Document, fields: "list[str]") -> float:
    """A small score boost when the page actually SHOWS the fields the brief's schema asks for --
    so Locate recognises the RIGHT dataset among several record lists (the schema is SHARED: Author
    extracts these fields, Locate uses them to find them). A tiebreaker, capped so record-presence
    still dominates; matched as whole words in the page/JSON text."""
    if not fields:
        return 0.0
    hay = doc.text.lower()
    tokens = set(re.findall(r"[a-z0-9]+", hay))
    hits = sum(1 for f in fields if all(t in tokens for t in re.findall(r"[a-z0-9]+", f.lower())))
    return min(2.0, hits * 0.5)


async def locate(
    brief: "LocateBrief | str",
    *,
    resolver: Resolver,
    search: "Search | None" = None,
    frontier: "tuple[FrontierMiddleware, ...]" = (),
    entity: str = "",
    review: "Llm | None" = None,
) -> "Reference | None":
    """Find the best source for the goal and return a :class:`Reference` (or ``None`` if nothing
    holds the dataset -- Locate is ALLOWED to fail). Pass a bare goal string for the common case.
    Seeds come from the brief, else ``search``; candidates from the brief, else a crawl; the winner
    is the highest dataset score (record-likeness + a schema-match bonus for the brief's fields),
    then the XHR/data-API preference is applied, then a FINAL REVIEW. An explicit source
    (seeds/candidates/start_url) makes ``search`` irrelevant -- it is only used when none is given.
    ``frontier`` is a turn-based crawl policy (an LLM / heuristic that picks which frontier URLs to
    expand); default is breadth-first. ``entity`` (the company/site the dataset belongs to) is not a
    hardcoded host rule -- it is CONTEXT handed to the LLM stages so THEY make the hostname call: the
    ``frontier`` LLM expands only the entity's own pages and rejects third-party aggregators / news,
    and the ``review`` LLM (below) confirms the winner is the entity's data. ``review`` (optional) is
    an LLM that judges each candidate best-first: it must confirm the page actually holds the
    requested dataset (for the entity) or Locate skips it -- so the model genuinely SELECTS the
    source, and Locate FAILS (``None``) if none survive."""
    lb = LocateBrief(goal=brief) if isinstance(brief, str) else brief
    seeds = list(lb.seeds)
    if lb.start_url and lb.start_url not in seeds:  # a known source to seed the crawl from
        seeds.append(lb.start_url)
    if (
        not lb.candidates and not seeds
    ):  # no explicit source -> search (the qualifier, else the goal)
        if search is None:
            raise ValueError("locate needs seeds, candidates, or a search callable")
        seeds = await search(lb.search or lb.goal)
    # Broad search often surfaces only third-party aggregators (Benzinga / MarketScreener / a stock
    # exchange) and misses the entity's OWN IR page. When a reviewer LLM + entity are given, ask the
    # model for the official IR URL and seed it FIRST -- prompt-driven (the model knows the domain),
    # not a hardcoded host rule. A wrong guess is harmless: the crawl only yields pages that fetch OK.
    if review is not None and entity and not lb.candidates:
        guess = await _official_ir_url(review, entity, lb.goal)
        if guess and guess not in seeds:
            seeds.insert(0, guess)
            emit(ReasonEvent(stage="seed", subject=guess, text=f"official IR page for {entity}"))

    if lb.candidates:  # status-aware: a blocked/errored candidate (403/5xx) is not a source
        docs = []
        for u in lb.candidates:
            snap = await resolver.snapshot(Request(url=u))
            if snap.ok:
                docs.append(document(snap))
    else:
        # The seeds (which came from a broad web search) sit UNFETCHED in the frontier, so the
        # entity-aware ``frontier`` LLM prunes off-entity ones before they're fetched -- Locate does
        # not hardcode which hosts belong to the company.
        goal = Goal(start=seeds, max_pages=lb.max_pages, frontier=frontier, assess=True)
        docs = [d async for d in Crawler(resolver).crawl(goal)]  # the crawl yields only OK pages

    scored: "list[tuple[float, Reference, Document]]" = []
    for doc in docs:
        by = {f.name: f for f in flags(doc)}
        score = _score(doc, by)
        if lb.download:  # a DOWNLOAD brief: a page that LISTS the target files IS the source
            targets = _download_targets(doc)
            if targets:
                score = max(score, 5.0 + min(len(targets), 20) * 0.1)
        if score <= 0.0:
            continue
        score += _field_bonus(doc, lb.fields)  # schema-match tiebreaker (the brief's fields)
        scored.append((score, _reference(doc, by), doc))

    # FINAL REVIEW, best-first: the top candidate is not accepted blindly. When a reviewer LLM is
    # given, IT decides whether each candidate truly holds the requested dataset (for the entity) --
    # the first it accepts wins; if it accepts none, Locate FAILS (None), rather than hand Author a
    # wrong page (the finrange.com/company/ACME aggregator). No reviewer -> take the top score.
    scored.sort(key=lambda t: t[0], reverse=True)
    for _s, ref, page in scored:
        if review is not None and not await _llm_review(review, lb, page, entity):
            continue  # the model rejected it as off-dataset / off-entity
        # the winner passed review -> work out how to actually LOAD its data (static / browser / API)
        # and bake it into the Reference, so Author uses the right transport instead of HTTP-by-default.
        return await _loading_requirements(page, ref, resolver, lb)
    return None  # no candidate survived review -- Locate is allowed to fail


async def _official_ir_url(llm: Llm, entity: str, goal: str) -> "str | None":
    """The model's best guess of ``entity``'s OWN investor-relations page URL -- a high-quality seed
    when a broad search returns only aggregators. Prompt-driven (not a host rule): the model knows
    the company's domain. ``None`` on an unusable reply; a wrong guess is harmless (the crawl only
    keeps pages that fetch OK)."""
    prompt = (
        f"What is the exact URL of {entity}'s OFFICIAL investor-relations page for '{goal or 'events'}'"
        f" -- on {entity}'s OWN corporate/IR domain, NOT a third-party aggregator (Benzinga, "
        f"MarketScreener, Yahoo Finance, Quartr, Seeking Alpha) or a stock exchange. Reply with ONLY "
        f"the URL (https://...), or the single word none if you are unsure."
    )
    try:
        reply = await llm.complete(prompt)
    except WebException:
        return None
    match = re.search(r"https?://\S+", reply)
    return match.group(0).rstrip(".,)]\"'") if match else None


async def _llm_review(llm: Llm, lb: "LocateBrief", page: Document, entity: str) -> bool:
    """The LLM verdict on ONE candidate: does this page actually hold the requested dataset -- and,
    when given, does it belong to ``entity`` (this is where the model, not a hardcoded rule, judges
    the host) rather than being a third party's page about it? Locate calls this best-first and takes
    the first the model accepts, so this is genuine candidate SELECTION, not a rubber stamp on the top
    score. The verdict streams as a :class:`ReasonEvent`. On a model error, default to accept (an LLM
    outage must not sink an otherwise plausible source)."""
    skel = (
        page.json_skeleton(max_lines=120)
        if page.kind == "json"
        else page.skeleton(max_lines=120, drop_chrome=True)
    )
    want = lb.goal or "the target dataset"
    scope = f" It must be {entity}'s OWN data, not a third party's page ABOUT it." if entity else ""
    fields = ("\nExpected fields per record: " + ", ".join(lb.fields)) if lb.fields else ""
    # Judge SOURCE fit, not single-page field completeness: a listing/index of the records IS the
    # right source even when per-record detail (documents, long descriptions) or a secondary section
    # (e.g. archived vs upcoming) is one link/sibling away -- the extractor follows those. Rejecting a
    # correct listing for "not every field is visible here" is the failure this guards against; the
    # entity check (own data vs a third party's page about it) stays.
    prompt = (
        f"You are choosing the SOURCE PAGE to extract a dataset from. We need this dataset: "
        f"{want}.{scope}{fields}\n\n"
        f"Candidate page ({page.url}):\n{skel}\n\n"
        "Judge whether this is the RIGHT SOURCE -- NOT whether every field is already visible here:\n"
        "- ACCEPT a page that LISTS or indexes the target records (a listing, calendar, table, or "
        "archive of them), even if the full per-record detail (documents, long descriptions, extra "
        "values) is reached by following each record's link, and even if a secondary section (e.g. "
        "archived vs upcoming) lives on a linked or sibling page -- the extractor follows those, you "
        "are only picking the entry page.\n"
        "- REJECT a page that shows only ONE record's detail when the goal asks for a set of them, a "
        "third party's page ABOUT the entity rather than its own data, or a page unrelated to the "
        "dataset.\n"
        "- Truncated HTML is expected -- do not reject for truncation or for detail that would live on "
        "a linked page.\n\n"
        f"Is this the right source page to extract '{want}' from"
        f"{f' for {entity}' if entity else ''}? Answer YES or NO on the first line, then one short "
        f"sentence why."
    )
    try:
        reply = await llm.complete(prompt)
    except WebException:
        return True
    first = next((ln for ln in reply.strip().splitlines() if ln.strip()), "").strip().lower()
    accept = first.startswith("yes") or (not first.startswith("no") and "yes" in first)
    emit(
        ReasonEvent(
            stage="review",
            subject=page.url,
            text=("accepted: " if accept else "rejected: ") + reply.strip()[:200],
        )
    )
    return accept


def _has_records(doc: Document, brief: "LocateBrief") -> bool:
    """Whether the TARGET records look present in ``doc`` -- a JSON data document is itself the data;
    a download brief is satisfied by download links; otherwise a repeating record region that also
    looks like the brief's dataset. A HINT, not a verdict: a repeating region ALONE is weak (nav
    menus, news teasers and related-link lists match too), so when the brief names fields it must be
    corroborated by a schema match (the page actually shows those fields) OR be a substantial list.
    The author loop does not trust this -- it renders + re-samples empirically if a query comes back
    empty -- so a false negative here just costs one extra browser check, never the dataset."""
    if doc.kind == "json":
        return True
    if brief.download:
        return len(_download_targets(doc)) > 0
    regions = doc.records(min_items=2)
    if not regions or regions[0].count < 2:
        return False
    if not brief.fields:  # nothing to corroborate against -> a repeating region is the best signal
        return True
    # a repeating region ALONE is not enough (a nav menu / news teaser / related-link list matches,
    # however long) -- require the page to actually SHOW the brief's fields, so a JS-gated shell whose
    # only static lists are chrome reads as "not present here" and Locate renders to check.
    return _field_bonus(doc, brief.fields) > 0.0


def _record_count(doc: Document) -> int:
    """The item count of the page's dominant repeating region (0 if none) -- the SIZE of the list a
    page carries. Comparing this static vs rendered is a schema-independent test of JS-gating: a
    server-rendered shell has few/no rows, and the render makes them appear."""
    regions = doc.records(min_items=1)
    return regions[0].count if regions else 0


async def _render_page(url: str, pool: ClientPool) -> Snapshot:
    """Render ``url`` in a real browser (the top realness tier) and return the snapshot -- how Locate
    sees a page as a BROWSER would, to detect JS-gating. A module-level seam so a test can stub the
    render (the comparison logic in :func:`_loading_requirements` is what's under test, not a live
    browser)."""
    browser = Resolver(profile=_rp.FULL_BROWSER, pool=pool)
    return await browser.snapshot(Request(url=url))


async def _loading_requirements(
    page: Document, ref: Reference, resolver: Resolver, brief: "LocateBrief"
) -> Reference:
    """Determine HOW to actually LOAD the dataset and bake it into the Reference -- so Author uses the
    right transport instead of HTTP-by-default.

    The static page is NOT trusted on its own: a JS app server-renders a thin shell (nav + a few
    teasers) whose GENERIC text passes a lenient static check while the REAL dataset is injected by
    script (no ``spa`` signal need fire). So RENDER the winner once (:func:`_render_page`) and decide
    EMPIRICALLY by comparing static vs rendered -- when the render reveals materially more of the
    dataset it is JS-gated, so bake ``full_browser`` and mine the XHR/fetch stream for the JSON
    data-API that feeds it (fetchable at ``basic``, cheaper). Rendering the single winner is cheap
    (the pool reuses the browser); best-effort -- no browser -> trust the static verdict."""
    static_ct = _record_count(page)
    static_ok = _has_records(page, brief)
    try:
        snap = await _render_page(page.url, resolver.pool)
    except Exception:  # no browser can launch here -> trust the static verdict (never sink Locate)
        ref = ref.model_copy(update={"profile": "basic"})
        return await _prefer_api(page, ref, resolver) if (brief.prefer_api and static_ok) else ref
    rendered = document(snap)
    rendered_ct = _record_count(rendered)
    # JS-gated iff the render revealed MORE of the dataset than the static HTML: the record count grew
    # materially (rows appeared that the static HTML lacked), OR the rendered page passes the schema
    # check while the static page did not. If neither, the static HTML already had it -- ``basic``.
    gained = rendered_ct >= 2 and rendered_ct > static_ct + 1
    js_gated = gained or (_has_records(rendered, brief) and not static_ok)
    if not js_gated:
        ref = ref.model_copy(update={"profile": "basic"})
        return await _prefer_api(page, ref, resolver) if brief.prefer_api else ref
    emit(
        ReasonEvent(
            stage="load",
            subject=page.url,
            text=f"JS-gated ({static_ct}→{rendered_ct} records on render) — needs a browser",
        )
    )
    ref = ref.model_copy(update={"profile": "full_browser", "needs_browser": True})
    for url, api in _json_xhr(snap):  # the JSON API the render actually called (reuse the render)
        if _consistent(rendered, api):
            emit(
                ReasonEvent(stage="load", subject=url, text="found the data-API — easier to scrape")
            )
            return ref.model_copy(
                update={"url": url, "kind": api.kind, "api_endpoint": url, "profile": "basic"}
            )
    return ref


__all__ = ["locate", "Search", "data_api_endpoints"]
