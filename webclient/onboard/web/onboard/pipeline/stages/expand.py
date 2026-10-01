"""Stage 5 -- expand: describe the located source from its SIGNALS -- the contract the author
reads. Mechanical: the record selector and count, the transport tier, the pagination / API /
order / filter / SPA descriptions each from the flag that fired. (Per-signal model explorations --
a pager's parameters, an API's knobs -- are later refinements of this stage.)"""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse

from pydantic import JsonValue
from web.fetch import Request, Snapshot, emit
from web.parse import Document
from web.resolve import Flag, Resolver, document, flags
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
from . import review_candidate as _rc

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


_YEAR = re.compile(r"^(?:FY\s*)?(19|20)\d{2}$")


def filters_of(doc: Document) -> "list[str]":
    """The year tabs / filters a page shows, DESCRIBED: which years are offered and which one is
    selected -- so the author knows the latest data is the current tab and older years sit in
    another container (often another format). A ``<select>`` of years, year tabs / links /
    buttons. Empty when none."""
    if doc.kind != "html":
        return []
    out: list[str] = []
    for sel in doc.select_all("select"):
        opts = [
            (o.text.strip(), o.attrs.get("selected") is not None) for o in sel.select_all("option")
        ]
        years = [(t, chosen) for t, chosen in opts if _YEAR.match(t)]
        if len(years) >= 2:
            out.append(
                "a year selector: "
                + ", ".join(f"{t}{' (selected)' if chosen else ''}" for t, chosen in years[:12])
            )
    tabs: list[tuple[str, bool]] = []
    for el in doc.select_all("[role=tab], a, button"):
        text = el.text.strip()
        if _YEAR.match(text):
            attrs = el.attrs
            chosen = attrs.get("aria-selected") == "true" or any(
                c in ("active", "selected", "current", "is-active")
                for c in attrs.get("class", "").split()
            )
            if text not in [t for t, _ in tabs]:
                tabs.append((text, chosen))
    if len(tabs) >= 2:
        out.append(
            "year tabs / links: "
            + ", ".join(f"{t}{' (selected)' if chosen else ''}" for t, chosen in tabs[:12])
            + " -- the other years' records are usually in a different container, often a different format"
        )
    return out


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


#: the signals that say a script builds the page: render once before trusting the HTTP tier.
_APP_SIGNALS = ("spa", "needs_browser", "data_api", "iframe")
#: a static page whose readable text is shorter than this is thin enough to render once.
_THIN_CHARS = 1_500


def render_worth(doc: Document, by: "dict[str, Flag]", brief: Brief) -> str:
    """Why the HTTP-tier page should be RENDERED once before trusting it: a script-app / data-API
    signal, or a thin page. ``""`` = trust it. (A description of the page, never a selector.)"""
    hot = [n for n in _APP_SIGNALS if n in by]
    if hot:
        return f"signals {', '.join(hot)}"
    if len(doc.readable()) < _THIN_CHARS:
        return "a thin page (the content may be injected by script)"
    return ""


async def run(state: Onboarding, ctx: Context) -> DatasetSource:
    assert state.review_candidate is not None and state.review_candidate.present
    review = state.review_candidate
    doc, snap = await page_of(ctx, review.url, review.profile)
    by = {f.name: f for f in flags(doc, snap)}
    profile = review.profile
    if doc.kind == "html" and profile == "basic":
        # EMPIRICAL loading requirements (a lesson kept): the static page is not trusted on its own
        # -- a script app server-renders a thin shell that passes a skeleton read while the dataset
        # is injected later. Render once on a signal and compare; prefer the feed the render called.
        why = render_worth(doc, by, state.brief)
        if why:
            emit(
                ReasonEvent(
                    stage="expand", subject=review.url, text=f"rendering once to compare ({why})"
                )
            )
            try:
                snap2 = await _rc.render(ctx, review.url)
            except Exception as exc:  # noqa: BLE001 -- no browser: the static page stands
                emit(ReasonEvent(stage="expand", text=f"no browser render: {exc}"))
            else:
                doc2 = document(snap2)
                static, rendered = len(doc.readable()), len(doc2.readable())
                feeds = observed_endpoints(snap2)
                emit(
                    ReasonEvent(
                        stage="expand",
                        text=f"rendered: {rendered} chars of content vs {static} static; "
                        f"{len(feeds)} json call(s) observed",
                    )
                )
                if rendered > max(static * 1.5, static + 800):
                    doc, snap, profile = doc2, snap2, "full_browser"
                    by = {f.name: f for f in flags(doc, snap)}
                    ctx.docs[review.url] = (doc, snap)
                elif feeds:  # the feed is what matters; the page may stay static
                    snap = snap2
    src = DatasetSource(
        url=review.url,
        kind=doc.kind,
        profile=profile,
        flags=sorted(by),
        filtered="tabbed" in by,
        filters=filters_of(doc),
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
    if any(n in by for n in ("needs_browser", "spa", "iframe")) or profile == "full_browser":
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
            text=f"{src.kind} at {src.profile}; flags {', '.join(src.flags) or 'none'}; {api_line}{pager}"
            + (f"spa ({src.spa.reason})" if src.spa is not None else "static"),
        )
    )
    return src
