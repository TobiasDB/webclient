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

from web.crawl import Crawler, Follow, FrontierMiddleware, Goal, same_origin
from web.fetch import FetchEvent, NetworkEvent, Request, Trace, WebException, emit
from web.parse import Document, parse
from web.resolve import Flag, Resolver, document, flags

from .llm import Llm, ReasonEvent
from .models import LocateBrief, Reference

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
    if "blocked" in by:  # an anti-bot wall / challenge page -- not the dataset (was picked wrongly)
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


async def _observed(page: Document, resolver: Resolver) -> "list[tuple[str, Document]]":
    """The same-origin XHR/``fetch`` responses the page ACTUALLY made whose captured body is JSON --
    the live data-API behind it, not a URL guessed from the DOM. It renders through the CALLER's
    resolver (:meth:`Resolver.snapshot`), so it captures a network stream only when the caller chose
    a browser profile; an HTTP-only resolver emits no network events and this returns ``[]`` (Locate
    never launches a browser on its own). Bodies are already drained onto the Snapshot -- no re-fetch.
    """
    snap = await resolver.snapshot(Request(url=page.url))
    host = urlparse(page.url).hostname
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


#: file extensions a download brief harvests (mirrors :mod:`web.onboard.author`).
_FILE_EXT = (
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


def _download_targets(doc: Document) -> "list[str]":
    """The downloadable-file links on the page (by extension) -- the 'records' of a download brief."""
    return [u for u in doc.links() if u.lower().split("?")[0].endswith(_FILE_EXT)]


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


#: corporate suffixes / stopwords dropped from an entity name before matching it to a hostname.
_ENTITY_STOP = frozenset(
    {
        "inc",
        "incorporated",
        "corp",
        "corporation",
        "co",
        "company",
        "ltd",
        "limited",
        "plc",
        "llc",
        "lp",
        "group",
        "holdings",
        "holding",
        "the",
        "and",
        "of",
        "for",
        "sa",
        "ag",
        "nv",
        "spa",
        "gmbh",
        "ab",
        "as",
        "oyj",
        "se",
        "kk",
        "pte",
        "bhd",
    }
)
#: multi-label public suffixes, so ``_registrable`` keeps ``acme.co.uk`` (not ``co.uk``).
_MULTI_TLD = frozenset(
    {
        "co.uk",
        "org.uk",
        "ac.uk",
        "gov.uk",
        "com.au",
        "net.au",
        "org.au",
        "co.jp",
        "co.nz",
        "com.br",
        "co.in",
        "co.za",
        "com.sg",
        "com.hk",
        "com.cn",
        "co.kr",
    }
)


def _entity_tokens(entity: str) -> "frozenset[str]":
    """The distinctive lowercase tokens of a company/entity name (corporate suffixes and short
    words dropped). Used to recognise the entity's OWN hostnames -- its website AND its name-based
    issuer subdomain on an IR-platform host (``acme.q4cdn.com``, ``acme.gcs-web.com``) -- so Locate
    stays on the company and rejects unrelated third-party sources (aggregators / news / exchanges
    that name it only in a URL path)."""
    return frozenset(
        t for t in re.findall(r"[a-z0-9]+", entity.lower()) if len(t) >= 3 and t not in _ENTITY_STOP
    )


def _registrable(url: str) -> str:
    """The registrable domain of ``url`` (eTLD+1, a small multi-label suffix list handled), lower.
    Approximate (no full public-suffix list) -- enough to keep the crawl within one company."""
    host = (urlparse(url).hostname or "").lower()
    parts = host.split(".")
    if len(parts) >= 3 and ".".join(parts[-2:]) in _MULTI_TLD:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def _on_entity(url: str, tokens: "frozenset[str]") -> bool:
    """Whether ``url``'s HOST belongs to the entity -- an entity-name token appears in the hostname
    (dots stripped). This matches the company's own domain and its name-based issuer subdomain on a
    shared IR host, but NOT an aggregator that carries the name only in the PATH -- the exact line
    the user drew (keep ``acme.q4cdn.com``, drop ``finrange.com/company/ACME``)."""
    host = (urlparse(url).hostname or "").lower().replace(".", "")
    return any(t in host for t in tokens)


def _entity_scope(seeds: "list[str]", tokens: "frozenset[str]") -> Follow:
    """A crawl traversal scope for an entity: follow a link iff it is on the same registrable domain
    as an (on-entity) seed OR its host itself carries the entity name -- so the crawl can hop from
    ``acme.com`` to ``acme.q4cdn.com`` but never wanders onto an unrelated third party."""
    domains = frozenset(_registrable(u) for u in seeds)

    def follow(doc: Document, link: str) -> bool:
        return _registrable(link) in domains or _on_entity(link, tokens)

    return follow


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
    expand); default is breadth-first. When ``entity`` is set (the company/site the dataset belongs
    to), Locate SCOPES to it: search seeds and the crawl stay on the entity's own domain(s), and the
    final review REJECTS an off-entity winner -- a third-party source (aggregator / news) that
    merely mentions the company is not a valid source, so Locate returns ``None`` rather than a wrong
    page. ``review`` (optional) is an LLM that judges each candidate best-first: it must confirm the
    page actually holds the requested dataset (for the entity) or Locate skips it -- so the model
    genuinely SELECTS the source, and Locate FAILS (``None``) if none survive."""
    lb = LocateBrief(goal=brief) if isinstance(brief, str) else brief
    tokens = _entity_tokens(entity)
    seeds = list(lb.seeds)
    if lb.start_url and lb.start_url not in seeds:  # a known source to seed the crawl from
        seeds.append(lb.start_url)
    searched = False
    if (
        not lb.candidates and not seeds
    ):  # no explicit source -> search (the qualifier, else the goal)
        if search is None:
            raise ValueError("locate needs seeds, candidates, or a search callable")
        seeds = await search(lb.search or lb.goal)
        searched = True

    # Entity scoping: when the seeds came from a broad web search, keep only those on the entity's
    # OWN host -- drop the third-party results (aggregators / news / exchanges) that the search mixes
    # in. Fall back to all seeds if none match (a company whose site omits its name), where the final
    # review still guards the winner.
    if tokens and searched:
        on_entity = [u for u in seeds if _on_entity(u, tokens)]
        if on_entity:
            seeds = on_entity

    if lb.candidates:  # status-aware: a blocked/errored candidate (403/5xx) is not a source
        docs = []
        for u in lb.candidates:
            snap = await resolver.snapshot(Request(url=u))
            if snap.ok:
                docs.append(document(snap))
    else:
        scope: Follow = _entity_scope(seeds, tokens) if tokens else same_origin
        goal = Goal(
            start=seeds, scope=scope, max_pages=lb.max_pages, frontier=frontier, assess=True
        )
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

    # FINAL REVIEW, best-first: the top candidate is not accepted blindly. Each must pass the
    # deterministic entity gate AND (when a reviewer LLM is given) the model's verdict that it truly
    # holds the requested dataset. The first that survives wins; if none do, Locate FAILS (None) --
    # better than handing Author a wrong page (the finrange.com/company/ACME aggregator).
    scored.sort(key=lambda t: t[0], reverse=True)
    for _s, ref, page in scored:
        if not _entity_relevant(page, tokens):  # cheap gate: off-entity third party -> skip
            continue
        if review is not None and not await _llm_review(review, lb, page, entity):
            continue  # the model rejected it as off-dataset / off-entity
        if lb.prefer_api and page.kind == "html":
            ref = await _prefer_api(page, ref, resolver)
        return ref.model_copy(update={"profile": await _working_profile(ref.url, resolver)})
    return None  # no candidate survived review -- Locate is allowed to fail


def _entity_relevant(page: Document, tokens: "frozenset[str]") -> bool:
    """The cheap final gate -- Locate is allowed to FAIL rather than hand Author a wrong page. When an
    entity was given, a candidate must actually BELONG to it: its host carries the entity name (its
    own site / issuer subdomain) OR the entity name appears in the page text. This rejects a
    high-scoring third-party aggregator (a ``record_list`` on ``finrange.com/company/ACME`` IS a
    record list, but not the COMPANY'S data). No entity -> nothing to check against, so pass."""
    if not tokens:
        return True
    if _on_entity(page.url, tokens):
        return True
    text_tokens = set(re.findall(r"[a-z0-9]+", page.text.lower()))
    return any(t in text_tokens for t in tokens)


async def _llm_review(llm: Llm, lb: "LocateBrief", page: Document, entity: str) -> bool:
    """The LLM verdict on ONE candidate: does this page actually hold the requested dataset -- and,
    when given, does it belong to ``entity`` rather than being a third party's page about it? Locate
    calls this best-first and takes the first the model accepts, so this is genuine candidate
    SELECTION, not a rubber stamp on the top score. The verdict streams as a :class:`ReasonEvent`.
    On a model error the deterministic gate already passed, so default to accept (an LLM outage must
    not sink an otherwise on-entity source)."""
    skel = (
        page.json_skeleton(max_lines=60)
        if page.kind == "json"
        else page.skeleton(max_lines=60, drop_chrome=True)
    )
    want = lb.goal or "the target dataset"
    scope = f" It must be {entity}'s OWN data, not a third party's page ABOUT it." if entity else ""
    fields = ("\nExpected fields per record: " + ", ".join(lb.fields)) if lb.fields else ""
    prompt = (
        f"You are validating a data source. We need this dataset: {want}.{scope}{fields}\n\n"
        f"Candidate page ({page.url}):\n{skel}\n\n"
        f"Does this page actually hold the requested dataset"
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


async def _working_profile(url: str, resolver: Resolver) -> str:
    """The transport that ACTUALLY fetched ``url``: ``full_browser`` if the resolve escalated to a
    browser tier, else ``basic`` (HTTP). One probe fetch, so Author bakes the profile that worked
    into the query root instead of re-running resolve's escalation discovery. (A static page never
    escalates, so it stays ``basic`` -- no browser is launched.)"""
    with Trace() as t:
        await resolver.snapshot(Request(url=url))
    browser = any(isinstance(e, FetchEvent) and e.source == "browser" for e in t.events)
    return "full_browser" if browser else "basic"


__all__ = ["locate", "Search", "data_api_endpoints"]
