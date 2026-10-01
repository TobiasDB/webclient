"""Stage 5 -- expand: describe the located source from its SIGNALS -- the contract the author
reads. Mechanical: the record selector and count, the transport tier, the pagination / API /
order / filter / SPA descriptions each from the flag that fired. (Per-signal model explorations --
a pager's parameters, an API's knobs -- are later refinements of this stage.)"""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse

from pydantic import JsonValue
from web.fetch import Request, Snapshot, emit
from web.parse import Document, Element
from web.resolve import Resolver, document, flags
from web.resolve import profiles as _rp

from ...llm import ReasonEvent
from ..apis import consistent, declared_endpoints, observed_endpoints, records_path, schema_fit
from ..ask import Context
from ..brief import Brief
from ..state import (
    ApiDescription,
    DatasetSource,
    Onboarding,
    PaginateDescription,
    SpaDescription,
)

_PAGERS = {"paginated": "next_link", "infinite_scroll": "scroll"}
#: query parameters a pager bumps, most common first.
_PAGE_PARAMS = ("page", "p", "pg", "pagenum", "page_number", "offset", "start", "skip")


def pager_of(doc: Document, kind: str, note: str) -> PaginateDescription:
    """How the pager works, from the DOM: a ``rel=next`` link (``next_link``); else a same-path link
    carrying a page parameter (``param`` -- its name); else a scroll / an unknown pager."""
    if kind == "scroll":
        return PaginateDescription(kind="scroll", note=note)
    if doc.select("a[rel=next], link[rel=next]") is not None:
        return PaginateDescription(kind="next_link", next_selector="a[rel=next]", note=note)
    path = urlparse(doc.url).path.rstrip("/")
    for link in doc.links():
        parts = urlparse(link)
        if parts.path.rstrip("/") != path:
            continue
        names = {k.lower(): k for k in parse_qs(parts.query)}
        for cand in _PAGE_PARAMS:
            if cand in names:
                return PaginateDescription(kind="param", param=names[cand], note=note)
    return PaginateDescription(kind="next_link", note=note)


_DATE = re.compile(
    r"\b(?:19|20)\d{2}\b|\b\d{1,2}[:.]\d{2}\b|\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\b",
    re.I,
)


def _hooks(el: Element) -> "set[str]":
    """Which field TYPES a record element can serve: a link (``url``), a date (``datetime``), text."""
    out: set[str] = set()
    if el.select("a[href]") is not None:
        out.add("url")
    if el.select("time") is not None or _DATE.search(el.text):
        out.add("datetime")
    if el.text.strip():
        out.add("string")
    return out


def pick_records(doc: Document, brief: Brief) -> "tuple[str, int]":
    """The record region FOR THE BRIEF: among the detected regions (merged by selector -- three
    sections of twelve are one dataset of thirty-six), the one whose typical record can serve the
    most of the brief's REQUIRED field types (a url field needs a link; a datetime needs a date),
    then the largest. A JSON document has no region (``("", 0)``)."""
    if doc.kind == "json":
        return "", 0
    merged: dict[str, int] = {}
    for reg in doc.records(top_k=6):
        merged[reg.item_selector] = merged.get(reg.item_selector, 0) + reg.count
    if not merged:
        return "", 0
    want = {f.type for f in brief.fields if not f.optional} & {"url", "datetime", "string"}
    best: "tuple[tuple[int, int], str] | None" = None
    for selector, count in merged.items():
        els = doc.select_all(selector)
        if not els:
            continue
        served = len(want & _hooks(els[min(len(els) - 1, 1)]))  # the second element: a typical one
        rank = (served, count)
        if best is None or rank > best[0]:
            best = (rank, selector)
    if best is None:
        return "", 0
    return best[1], len(doc.select_all(best[1]))


def knobs_of(url: str) -> "dict[str, str]":
    """An endpoint's query parameters as called (the knobs a feed exposes: page / year / type)."""
    return {k: v[0] for k, v in parse_qs(urlparse(url).query).items() if v}


async def page_of(ctx: Context, url: str, profile: str) -> "tuple[Document, Snapshot]":
    """The reviewed page from the run cache, else fetched again at ``profile`` (a resume)."""
    cached = ctx.docs.get(url)
    if (
        isinstance(cached, tuple)
        and isinstance(cached[0], Document)
        and isinstance(cached[1], Snapshot)
    ):
        return cached[0], cached[1]
    prof = _rp.get(profile) or _rp.BASIC
    snap = await Resolver(profile=prof, pool=ctx.resolver.pool).snapshot(Request(url=url))
    doc = document(snap)
    ctx.docs[url] = (doc, snap)
    return doc, snap


async def run(state: Onboarding, ctx: Context) -> DatasetSource:
    assert state.review_candidate is not None and state.review_candidate.present
    review = state.review_candidate
    doc, snap = await page_of(ctx, review.url, review.profile)
    by = {f.name: f for f in flags(doc, snap)}
    selector, count = pick_records(doc, state.brief)
    src = DatasetSource(
        url=review.url,
        kind=doc.kind,
        profile=review.profile,
        record_selector=selector,
        records=count,
        flags=sorted(by),
        filtered="tabbed" in by,
    )
    for flag, kind in _PAGERS.items():
        if flag in by:
            nxt = doc.select("a[rel=next], link[rel=next]")
            src.pagination = PaginateDescription(
                kind=kind,
                next_selector="a[rel=next]" if nxt is not None else "",
                note=by[flag].description,
            )
            break
    if any(n in by for n in ("needs_browser", "spa", "iframe")) or review.profile == "full_browser":
        src.spa = SpaDescription(
            profile="full_browser",
            reason=", ".join(n for n in ("needs_browser", "spa", "iframe") if n in by)
            or "reviewed through a browser",
        )
    # the data API behind the page: a declared endpoint, else (after a render) the XHR stream --
    # consistent with the page, best schema fit wins
    best: "tuple[int, str, Document] | None" = None
    found: list[tuple[str, Document]] = []
    for url in declared_endpoints(doc)[:4]:
        try:
            api = await ctx.resolver.resolve(url)
        except Exception:  # noqa: BLE001 -- a dead declared endpoint is simply not the API
            continue
        found.append((url, api))
    found.extend(observed_endpoints(snap))
    for url, api in found:
        if api.kind == "json" and consistent(doc, api):
            fit = schema_fit(api, state.brief)
            if best is None or fit > best[0]:
                best = (fit, url, api)
    if best is not None:
        fit, url, api = best
        src.api = ApiDescription(
            url=url, kind="json", records_path=records_path(api.json()), fit=fit
        )
    signals: list[JsonValue] = [
        str(n) for n in sorted({s.name for f in by.values() for s in f.signals})
    ]
    src.detail = {"title": doc.metadata().title or "", "signals": signals}
    api_line = (
        f"api {src.api.url} (fit {src.api.fit}); " if src.api is not None else "no data api; "
    )
    pager = (
        f"pager {src.pagination.kind} {src.pagination.next_selector or src.pagination.param}; "
        if src.pagination is not None
        else "no pager; "
    )
    emit(
        ReasonEvent(
            stage="expand",
            subject=src.url,
            text=f"{src.kind} at {src.profile}: {src.records} record(s) at {src.record_selector!r}; "
            f"flags {', '.join(src.flags) or 'none'}; {api_line}{pager}"
            + (f"spa ({src.spa.reason})" if src.spa is not None else "static"),
        )
    )
    return src
