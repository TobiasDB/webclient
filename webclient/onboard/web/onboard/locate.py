"""Locate -- ``goal -> Reference``: find WHERE the dataset lives and hand Author the hints.

The pipeline is explicit STAGES, each cheap before the next is expensive, and each with a defined
"what the model sees" (see the stage modules):

  1. **search**   (:mod:`.search`)   -- seed URLs by WEB SEARCH (never a guessed URL), then a
     metadata-only VERIFY that each result belongs to the entity. Skipped when the brief names
     ``seeds`` / ``candidates`` / ``start_url``.
  2. **crawl**    (:mod:`web.crawl`)  -- reach candidate pages from the seeds; the model steers the
     frontier from ``url + link text`` only (:func:`~web.onboard.frontier.llm_frontier`). Skipped
     when the brief names ``candidates`` (they are fetched directly).
  3. **select**   (:mod:`.select`)   -- ONE metadata-only call over all crawled pages
     (``url/title/kind/flags``) -> must/should/could candidates. Deterministic gates first.
  4. **evaluate** (:mod:`.evaluate`) -- the ONLY stage that shows the model a (clipped) skeleton,
     best-tier-first with early exit; flags stay ground truth. The winner's page.
  5. **load**     (here)              -- HOW to load the winner's data: render it once and compare
     static vs rendered (JS-gated -> a browser profile), and prefer a consistent same-origin JSON
     data-API over scraping the HTML. Baked into the :class:`Reference` so Author never guesses
     the transport.

Deterministic without a model (the signals + scores decide); with one, the model SELECTS and
JUDGES over real fetched pages. Locate is ALLOWED to fail -- ``None`` when nothing holds the data.
"""

from __future__ import annotations

import re

from urllib.parse import urlparse

from web.crawl import Crawler, FrontierMiddleware, Goal
from web.fetch import ClientPool, NetworkEvent, Request, Snapshot, emit
from web.parse import Document, parse
from web.resolve import Resolver, document, flags
from web.resolve import profiles as _rp

from .evaluate import (
    data_api_endpoints,
    download_targets,
    evaluate_candidates,
    field_bonus,
    reference,
)
from .llm import Llm, ReasonEvent
from .models import in_range, LocateBrief, Reference
from .search import Search, search_web
from .select import select_candidates


async def locate(
    brief: "LocateBrief | str",
    *,
    resolver: Resolver,
    search: "Search | None" = None,
    frontier: "tuple[FrontierMiddleware, ...]" = (),
    entity: str = "",
    review: "Llm | None" = None,
) -> "Reference | None":
    """Run the stages (module docstring) and return the :class:`Reference` for the best source, or
    ``None`` if nothing holds the dataset. Pass a bare goal string for the common case. ``search``
    supplies seeds when the brief names no explicit source; ``frontier`` steers the crawl (an LLM /
    heuristic; default breadth-first); ``entity`` is the company/site the dataset belongs to --
    CONTEXT handed to the model stages (verify / frontier / evaluate), never a hardcoded host rule;
    ``review`` is the model that drives verify + select + evaluate (``None`` -> deterministic)."""
    lb = LocateBrief(goal=brief) if isinstance(brief, str) else brief
    llm = review  # the one model for the judgement stages (verify / select / evaluate)

    # -- 1. search ----------------------------------------------------------------------------
    seeds = list(lb.seeds)
    if lb.start_url and lb.start_url not in seeds:  # a known source to seed the crawl from
        seeds.append(lb.start_url)
    if not lb.candidates and not seeds:
        if search is None:
            raise ValueError("locate needs seeds, candidates, or a search callable")
        seeds = [h.url for h in await search_web(lb, entity, search=search, llm=llm)]
        if not seeds:
            emit(ReasonEvent(stage="search", text="no search seeds — nothing to crawl"))
            return None

    # -- 2. crawl (or fetch the explicit candidates) ------------------------------------------
    docs: list[Document] = []
    if lb.candidates:  # status-aware: a blocked/errored candidate (403/5xx) is not a source
        for u in lb.candidates:
            snap = await resolver.snapshot(Request(url=u))
            if snap.ok:
                docs.append(document(snap))
    else:
        # the seeds sit UNFETCHED in the frontier, so the entity-aware frontier policy prunes
        # off-entity ones before they cost a fetch; the crawl yields only OK pages.
        goal = Goal(start=seeds, max_pages=lb.max_pages, frontier=frontier, assess=True)
        docs = [d async for d in Crawler(resolver).crawl(goal)]
    if not docs:
        emit(ReasonEvent(stage="crawl", text="no page fetched OK — nothing to select from"))
        return None
    by_url = {d.url: d for d in docs}

    # -- 3. select ----------------------------------------------------------------------------
    candidates = await select_candidates(docs, lb, llm=llm, seed_urls=seeds)
    if not candidates:
        emit(ReasonEvent(stage="select", text="no candidate pages (every page gated out)"))
        return None

    # -- 4. evaluate (best-tier-first, early exit on the ideal case) --------------------------
    picked = await evaluate_candidates(candidates, by_url, lb, llm=llm, entity=entity)
    if picked is None:
        emit(ReasonEvent(stage="evaluate", text="no candidate holds the dataset"))
        return None
    ev, cand = picked
    if ev.exit_when_met:  # the brief's exit condition holds -> a CLEAN stop, not a failure
        emit(
            ReasonEvent(
                stage="evaluate",
                subject=ev.url,
                text=f"exit condition met — {ev.exit_reason or lb.exit_when}",
            )
        )
        return None
    page = by_url[ev.url]
    ref = reference(page, {f.name: f for f in flags(page)})
    ref = ref.model_copy(  # carry the judgement into the Reference: the summary + Author read it
        update={
            "detail": {
                **ref.detail,
                "tier": cand.tier,
                "verdict": ev.verdict,
                "scrapability": ev.scrapability,
                "queryable": ev.is_queryable,
                "sort_order": ev.sort_order or "",
                "recency_hint": ev.recency_hint,
                "completeness": ev.completeness or "",
            }
        }
    )

    # -- 5. load: how to actually reach the data (transport + a data-API, if any) ------------
    return await _loading_requirements(page, ref, resolver, lb)


# -- the load stage -------------------------------------------------------------------------------


def _spa_signalled(ref: Reference) -> bool:
    """Whether the page's detections carry the JS-app SIGNAL (``spa``, or an ``iframe`` that hides
    the content) -- the ground truth that it needs a browser to build its DOM."""
    return any(
        f.name in ("spa", "iframe") or any(s.name == "spa" for s in f.signals)
        for f in ref.assessment
        if f.present
    )


def _consistent(page: Document, api: Document) -> bool:
    """Whether ``api`` (a JSON response) BACKS ``page``: enough of its distinctive scalar leaves
    appear in the page's text. Guards against preferring an unrelated feed / a stale endpoint."""
    if api.kind != "json":
        return False
    leaves = [v for v in api.json_leaves(budget=2000) if len(v) >= 3][:40]
    if not leaves:
        return False
    text = page.text.lower()
    hits = sum(1 for v in leaves if v.lower() in text)
    return hits >= max(2, len(leaves) // 4)


_STOP = frozenset(
    "the a an of to for and or in on at by with its if is as from that this each per".split()
)


def _words(*texts: str) -> "set[str]":
    """Lower-cased word tokens (camelCase split) of some texts, minus stopwords and short bits."""
    out: set[str] = set()
    for t in texts:
        t = re.sub(r"([a-z])([A-Z])", r"\1 \2", t)
        out.update(w for w in re.findall(r"[a-z0-9]+", t.lower()) if len(w) > 2 and w not in _STOP)
    return out


def _json_keys(value: object, out: "set[str]", depth: int = 0) -> None:
    if depth > 6:
        return
    if isinstance(value, dict):
        for k, v in value.items():
            out.add(str(k))
            _json_keys(v, out, depth + 1)
    elif isinstance(value, list):
        for v in value[:5]:
            _json_keys(v, out, depth + 1)


def schema_fit(api: Document, brief: "LocateBrief") -> int:
    """How well a JSON API's KEY NAMES cover the brief's schema (field names + descriptions) --
    the event feed beats the presentation feed for an events brief, whatever their sizes."""
    keys: set[str] = set()
    _json_keys(api.json(), keys)
    key_words = _words(*keys)
    want = _words(*brief.fields, *brief.descriptions.values())
    return len(want & key_words)


# PLANNED PIPELINE STEP (out of scope for now -- USER DIRECTION 2026-10-01): "API DISCOVERY".
# A data feed the render calls is one SLICE of the dataset: Pfizer's events come from
# feed/Event.svc/GetEventList?eventSelection=1 (upcoming) and ...=0&year=2026 (past), each with
# page / pageSize / year / type / sort parameters. A later Locate stage should DISCOVER the feed's
# parameters (what each query-string knob selects; the full dataset = the union of slices), its
# ORDERING and TIMELINESS (newest first? paged?), its FILTERS (year / type / search), and express
# the dataset as SECTIONS across feeds -- so a split dataset is authored once per slice and the
# runner concatenates. Until then :func:`best_api` picks ONE feed: the one that fits the brief.
def best_api(
    page: Document, snap: Snapshot, brief: "LocateBrief | None" = None
) -> "tuple[str, Document] | None":
    """The same-origin JSON data-API the render called that is CONSISTENT with the page (its values
    appear in the page text) and FITS the brief's schema best (its key names cover the fields; ties
    broken by richness) -- the dataset's own feed, preferred over the HTML."""
    best: "tuple[tuple[int, int], str, Document] | None" = None
    for url, api in _json_xhr(snap):
        if not _consistent(page, api):
            continue
        rank = (
            schema_fit(api, brief) if brief is not None else 0,
            len(api.json_leaves(budget=20000)),
        )
        if best is None or rank > best[0]:
            best = (rank, url, api)
    return (best[1], best[2]) if best is not None else None


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
    (a browser profile) -- see :func:`_json_xhr`."""
    return _json_xhr(await resolver.snapshot(Request(url=page.url)))


async def _prefer_api(page: Document, ref: Reference, resolver: Resolver) -> Reference:
    """Apply the XHR-preference rule: root the Reference at a same-origin JSON data-API that is
    CONSISTENT with the page, instead of the page. Never a blind/hallucinated pick -- the endpoint's
    data is checked against the page first. Two passes: the cheap DOM-declared/link endpoints, then
    -- only when the page is JS-gated -- the real network stream it fires in a browser. The first
    consistent JSON source wins. A plain static page never launches a browser here."""
    for url in data_api_endpoints(page):
        api = await resolver.resolve(Request(url=url))
        if _consistent(page, api):
            return ref.model_copy(update={"url": url, "kind": api.kind, "api_endpoint": url})
    if ref.needs_browser:  # a JS-gated page: mine the XHR/fetch stream for the API that feeds it
        for url, api in await _observed(page, resolver):
            if _consistent(page, api):
                return ref.model_copy(update={"url": url, "kind": api.kind, "api_endpoint": url})
    return ref


def _has_records(doc: Document, brief: "LocateBrief") -> bool:
    """Whether the TARGET records look present in ``doc`` -- a JSON data document is itself the data;
    a download brief is satisfied by download links; otherwise a repeating record region that also
    looks like the brief's dataset. A HINT, not a verdict: a repeating region ALONE is weak (nav
    menus, news teasers and related-link lists match too), so when the brief names fields it must be
    corroborated by a schema match (the page actually shows those fields)."""
    if doc.kind == "json":
        return True
    if brief.download:
        return len(download_targets(doc)) > 0
    regions = doc.records(min_items=2)
    if not regions or regions[0].count < 2:
        return False
    if not brief.fields:  # nothing to corroborate against -> a repeating region is the best signal
        return True
    return field_bonus(doc, brief.fields) > 0.0


def _record_count(doc: Document) -> int:
    """The item count of the page's dominant repeating region (0 if none) -- the SIZE of the list a
    page carries. Comparing this static vs rendered is a schema-independent test of JS-gating: a
    server-rendered shell has few/no rows, and the render makes them appear."""
    regions = doc.records(min_items=1)
    return regions[0].count if regions else 0


def _show_range(bounds: "tuple[int, int] | None") -> str:
    if bounds is None:
        return "any"
    return f">= {bounds[0]}" if bounds[1] >= 10**9 else f"{bounds[0]}-{bounds[1]}"


#: words in an evaluate verdict that say the records are injected by script.
_RENDER_WORDS = re.compile(
    r"(requires?|needs?|must be|only after|loaded|injected|populated|rendered)\s+(?:\w+\s+){0,3}?"
    r"(browser|render\w*|javascript|\bjs\b|client-side|dynamic\w*|ajax|xhr|script)"
    r"|javascript[- ]rendered|client-side[- ]rendered|dynamically[- ](loaded|rendered|injected)"
    r"|needs? a browser|requires? (a )?(headless )?browser|loads? (via|through|by) (ajax|xhr|js)",
    re.I,
)
_NEGATED = re.compile(r"\b(no|not|without|doesn'?t|does not|never|isn'?t|is not)\b", re.I)


def _says_render(text: str) -> bool:
    """Whether an evaluate verdict POSITIVELY says the records are script-rendered -- a phrase such
    as "requires rendering" / "loaded by JavaScript" / "needs a browser", and not negated within
    the preceding few words ("no rendering needed", "does not require JavaScript")."""
    for m in _RENDER_WORDS.finditer(text):
        before = text[max(0, m.start() - 24) : m.start()]
        if not _NEGATED.search(before):
            return True
    return False


async def _render_page(url: str, pool: ClientPool) -> Snapshot:
    """Render ``url`` in a real browser (the top realness tier) and return the snapshot -- how Locate
    sees a page as a BROWSER would, to detect JS-gating. A module-level seam so a test can stub the
    render (the comparison logic in :func:`_loading_requirements` is what's under test)."""
    browser = Resolver(profile=_rp.FULL_BROWSER, pool=pool)
    return await browser.snapshot(Request(url=url))


async def _loading_requirements(
    page: Document, ref: Reference, resolver: Resolver, brief: "LocateBrief"
) -> Reference:
    """Determine HOW to actually LOAD the dataset and bake it into the Reference -- so Author uses the
    right transport instead of HTTP-by-default.

    The static page is NOT trusted on its own: a JS app server-renders a thin shell (nav + a few
    teasers) whose GENERIC text passes a lenient static check while the REAL dataset is injected by
    script. So RENDER the winner once (:func:`_render_page`) and decide EMPIRICALLY by comparing
    static vs rendered -- when the render reveals materially more of the dataset it is JS-gated, so
    bake ``full_browser`` and mine the XHR/fetch stream for the JSON data-API that feeds it
    (fetchable at ``basic``, cheaper). Rendering the single winner is cheap (the pool reuses the
    browser); best-effort -- no browser -> trust the static verdict."""
    # the page's OWN detections say JS-app: the `spa` / `iframe` SIGNAL (ground truth -- the same
    # rule the resolve ladder climbs a browser on), NOT the `needs_browser` conclusion alone, which
    # also fires on `empty` (any small page) and would over-bake browsers.
    signalled = _spa_signalled(ref)
    static_ct = _record_count(page)
    static_ok = _has_records(page, brief)
    try:
        snap = await _render_page(page.url, resolver.pool)
    except Exception as exc:  # the render failed -> never sink Locate, but never a SILENT profile
        verdict_says_render = _says_render(str(ref.detail.get("reason") or ""))
        if signalled or verdict_says_render:  # the page's signals / the evaluate verdict say JS
            why = (
                "the page's signals say JS-app (SPA)"
                if signalled
                else "the evaluate verdict says the records need rendering"
            )
            emit(
                ReasonEvent(
                    stage="load",
                    subject=page.url,
                    text=f"{why} — baking a browser profile (the confirming render failed: "
                    f"{type(exc).__name__}: {str(exc)[:120]})",
                )
            )
            return ref.model_copy(update={"profile": "full_browser", "needs_browser": True})
        emit(
            ReasonEvent(
                stage="load",
                subject=page.url,
                text=f"render failed ({type(exc).__name__}: {str(exc)[:120]}) — trusting the "
                f"static page ({static_ct} records; schema {'found' if static_ok else 'not found'})"
                " — profile basic",
            )
        )
        ref = ref.model_copy(update={"profile": "basic"})
        return await _prefer_api(page, ref, resolver) if (brief.prefer_api and static_ok) else ref
    rendered = document(snap)
    rendered_ct = _record_count(rendered)
    # JS-gated iff the page's signals SAY it is a JS app (flags are ground truth -- a "lowest tier
    # that worked" must never win over a detected SPA), OR the render revealed MORE of the dataset
    # than the static HTML (a server-rendered shell: the record count grew materially, or the
    # rendered page passes the schema check while the static page did not).
    gained = rendered_ct >= 2 and rendered_ct > static_ct + 1
    # the brief's expected range is a FLEXIBLE guide (a record count is never a hard rule): a static
    # page BELOW it whose render is NOT below it is gated, whatever the dominant region counted
    bounds = brief.expected_range()
    below_static = in_range(static_ct, bounds) is not None and static_ct < (bounds or (0, 0))[0]
    within_rendered = in_range(rendered_ct, bounds) is None
    # the evaluate VERDICT (the model read the skeleton and says the records are script-rendered)
    # WINS over the count heuristic: a record count is a flexible guide, never the rule that bakes
    # HTTP for such a page (USER: "11 marked as needing a browser chose basic"). The `needs_browser`
    # CONCLUSION is deliberately not used: it fires on `empty` (any small page) and over-bakes.
    verdict_says_render = _says_render(str(ref.detail.get("reason") or ""))
    js_gated = (
        signalled
        or gained
        or verdict_says_render
        or (_has_records(rendered, brief) and not static_ok)
        or (below_static and within_rendered)
    )
    if not js_gated:
        emit(
            ReasonEvent(
                stage="load",
                subject=page.url,
                text=f"rendered and compared: {static_ct} static vs {rendered_ct} rendered records, "
                f"schema {'found' if static_ok else 'not found'} statically"
                + (f"; note: {in_range(static_ct, bounds)}" if in_range(static_ct, bounds) else "")
                + " — not JS-gated, profile basic",
            )
        )
        ref = ref.model_copy(update={"profile": "basic"})
        if brief.prefer_api:  # the render's XHR stream is in hand: a consistent JSON API wins
            best = best_api(rendered, snap, brief)
            if best is not None:
                url, api = best
                emit(
                    ReasonEvent(
                        stage="load",
                        subject=url,
                        text="the render called a JSON data-API consistent with the page — rooting "
                        "the reference at it (the HTML lists only part of the dataset)",
                    )
                )
                return ref.model_copy(update={"url": url, "kind": api.kind, "api_endpoint": url})
            return await _prefer_api(page, ref, resolver)
        return ref
    why = (
        "the page's signals say JS-app (SPA)"
        if signalled
        else (
            f"the render revealed the dataset ({static_ct}→{rendered_ct} records"
            + (f"; the brief expects {_show_range(bounds)}" if bounds else "")
            + ")"
            if gained or (below_static and within_rendered)
            else (
                "the evaluate verdict says the records need rendering"
                if verdict_says_render
                else f"the render shows the schema the static page lacks ({static_ct}→{rendered_ct})"
            )
        )
    )
    emit(ReasonEvent(stage="load", subject=page.url, text=f"JS-gated — {why} — needs a browser"))
    ref = ref.model_copy(update={"profile": "full_browser", "needs_browser": True})
    best = best_api(rendered, snap, brief)  # the JSON API the render called (reuse the render)
    if best is not None:
        url, api = best
        emit(ReasonEvent(stage="load", subject=url, text="found the data-API — easier to scrape"))
        return ref.model_copy(
            update={"url": url, "kind": api.kind, "api_endpoint": url, "profile": "basic"}
        )
    return ref


__all__ = ["locate", "Search", "data_api_endpoints"]
