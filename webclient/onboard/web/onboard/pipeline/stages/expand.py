"""Stage 5 -- expand: describe the located source from its SIGNALS -- the contract the author
reads. Mechanical: the record selector and count, the transport tier, the pagination / API /
order / filter / SPA descriptions each from the flag that fired. (Per-signal model explorations --
a pager's parameters, an API's knobs -- are later refinements of this stage.)"""

from __future__ import annotations

from pydantic import JsonValue
from web.fetch import Request, Snapshot
from web.parse import Document
from web.resolve import Resolver, document, flags
from web.resolve import profiles as _rp

from ..apis import consistent, declared_endpoints, observed_endpoints, records_path, schema_fit
from ..ask import Context
from ..state import (
    ApiDescription,
    DatasetSource,
    Onboarding,
    PaginateDescription,
    SpaDescription,
)
from .review_candidate import record_count

_PAGERS = {"paginated": "next_link", "infinite_scroll": "scroll"}


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
    regions = doc.records(top_k=1)
    src = DatasetSource(
        url=review.url,
        kind=doc.kind,
        profile=review.profile,
        record_selector=regions[0].item_selector if regions else "",
        records=record_count(doc),
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
    return src
