"""Facet-only detectors: the rendered/network SPA signals and the tree-based flags
(pagination / forms / buttons). Imported by the ``flags`` facet, which fills the
``Context``'s ``tree`` / ``events`` / ``render_stats``; each detector self-skips
(returns ``None``) when its inputs are absent, so a request/static context is safe.

No lxml at import time -- the tree arrives on the context and ``cssselect`` is a
method on it -- so importing this module stays remote-safe.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime, timezone
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import parse_qsl, urljoin, urlparse

from ..dom import norm
from .context import Context
from .registry import Hit, detector, flag

if TYPE_CHECKING:
    from ..core.document.models import Form, Signal

_log = logging.getLogger(__name__)

_SPA_RATIO = 0.4  # injected-text share that alone marks a SPA
_SPA_MAIN_RATIO = 0.15  # lower bar when the injection is main-area + same-origin XHR


def _xhr_events(ctx: Context) -> list[Any]:
    """The captured XHR/fetch network events (empty without a browser render)."""
    return [e for e in ctx.events if getattr(e, "resource_type", None) in ("xhr", "fetch")]


#: URL fragments that mark an XHR/fetch as a DATA endpoint (an API returning records),
#: not a page asset -- so a cross-origin content service (a CaaS/CDN) can be told apart
#: from analytics/ad calls.
_DATA_HINTS = (
    "/api", "/graphql", "/gql", "/caas", "/content", "/feed", "/query", "/search",
    "/rest", "/v1/", "/v2/", "/v3/", "/_next/data", "/wp-json", ".json",
)
#: hosts whose cross-origin XHR is analytics/ads/tag-management, never the page's data.
_ANALYTICS_HOSTS = (
    "google-analytics", "googletagmanager", "google.com/ads", "doubleclick",
    "facebook", "segment.", "mixpanel", "hotjar", "optimizely", "adobedtm",
    "demdex", "omtrdc", "scorecardresearch", "quantserve", "amplitude",
)


def _is_data_endpoint(url: str) -> bool:
    """Whether a cross-origin XHR/fetch URL looks like a records/content API (a CaaS/CDN
    data source) rather than analytics or an asset -- so composing main content from it
    still reads as an SPA."""
    low = url.lower()
    host = (urlparse(low).hostname or "")
    if any(a in host or a in low for a in _ANALYTICS_HOSTS):
        return False
    return any(h in low for h in _DATA_HINTS)


def _injection(ctx: Context) -> "tuple[float, bool, int, int]":
    """``(injected_ratio, injected_in_main, same_origin_xhr, cross_origin_data_xhr)`` from
    the render. ``injected_ratio`` = NET text grown past the DOMContentLoaded baseline /
    final text; the last count is cross-origin XHR that look like DATA endpoints (a CaaS/
    CDN content API). All zero without a browser render."""
    mutations = [e for e in ctx.events if getattr(e, "kind", None) is not None and hasattr(e, "detail")]
    load_added = [
        e for e in mutations
        if getattr(e, "kind", None) == "added" and (getattr(e, "detail", None) or {}).get("phase") == "load"
    ]
    in_main = any((getattr(e, "detail", None) or {}).get("inMain") for e in load_added)
    page_host = (urlparse(ctx.final_url or ctx.url).hostname or "").lower()
    same_origin = cross_data = 0
    for e in _xhr_events(ctx):
        req = getattr(e, "request", None)
        if req is None:
            continue
        try:
            u = str(req.dispatch("url"))
        except Exception as exc:  # a malformed request event -- don't let it flip SPA silently
            _log.debug("dropped an XHR event with an unreadable url: %s", exc)
            continue
        host = (urlparse(u).hostname or "").lower()
        if not host:
            continue
        if host == page_host:
            same_origin += 1
        elif _is_data_endpoint(u):
            cross_data += 1
    stats = ctx.render_stats or {}
    total = int(stats.get("text", 0)) or len(norm(ctx.text))
    if stats.get("dclText") is not None and total:
        ratio = round(max(0, total - int(stats["dclText"])) / total, 3)
    else:
        ratio = 0.0
    return ratio, in_main, same_origin, cross_data


# -- spa: rendered + network evidence (joins the static signals in request_static) --


@detector(flag="spa", name="body_injected", stage="rendered")
def _body_injected(ctx: Context) -> Hit | None:
    """SPA evidence (rendered): a large share of the page's text was injected after the initial
    response -- the content is built client-side."""
    ratio, _, _, _ = _injection(ctx)
    if ratio >= _SPA_RATIO:
        return Hit(0.9, f"{ratio:.0%} of the page's text was injected after the initial response", ratio)
    return None


@detector(flag="spa", name="xhr_composed", stage="network")
def _xhr_composed(ctx: Context) -> Hit | None:
    """SPA evidence (network, strong): main content composed from same-origin XHR/fetch calls --
    the page's real data comes from its own API."""
    ratio, in_main, same_origin, _ = _injection(ctx)
    if in_main and same_origin >= 1 and ratio >= _SPA_MAIN_RATIO:
        return Hit(0.95, f"main content composed from {same_origin} same-origin XHR call(s)", same_origin)
    return None


def _data_endpoint_urls(ctx: Context) -> "list[str]":
    """The live DATA-API URLs the page CALLED: every same-origin XHR/fetch, plus cross-origin calls
    that look like a records/content endpoint (not analytics/ads/assets). Deduped, order preserved.
    Empty without a browser render (an XHR is only observed live)."""
    page_host = (urlparse(ctx.final_url or ctx.url).hostname or "").lower()
    out: list[str] = []
    for e in _xhr_events(ctx):
        req = getattr(e, "request", None)
        if req is None:
            continue
        try:
            u = str(req.dispatch("url"))
        except Exception as exc:  # noqa: BLE001 - a malformed request event never breaks detection
            _log.debug("data_api: dropped an XHR event with an unreadable url: %s", exc)
            continue
        host = (urlparse(u).hostname or "").lower()
        if host and (host == page_host or _is_data_endpoint(u)) and u not in out:
            out.append(u)
    return out


@detector(flag="data_api", name="data_endpoint_called", stage="network")
def _data_endpoint_called(ctx: Context) -> "Hit | None":
    """data_api evidence: the page CALLED a live JSON data API -- a same-origin XHR/fetch, or a
    cross-origin call to a records/content endpoint (not analytics). The whole dataset often lives
    behind it, so it is a strong hint the real source is the API (queryable, complete) rather than
    the rendered DOM."""
    n = len(_data_endpoint_urls(ctx))
    return Hit(0.85, f"the page called {n} live data endpoint(s)", n) if n else None


def _data_api_value(signals: "list[Signal]", ctx: Context) -> Any:
    """The ``data_api`` flag's value: the live data-endpoint URLs the page called (capped), so the UI
    can name them and the pipeline can target one directly rather than scraping the rendered DOM."""
    return _data_endpoint_urls(ctx)[:20] or None


flag("data_api", value=_data_api_value)


@detector(flag="spa", name="xhr_composed_cross_origin", stage="network")
def _xhr_composed_cross_origin(ctx: Context) -> Hit | None:
    """SPA evidence (network): main content composed from a CROSS-origin DATA endpoint (a CaaS/CDN
    content API) -- still an SPA, at lower confidence than the same-origin case."""
    # main content built from a CROSS-origin data endpoint (a CaaS/CDN content API, e.g.
    # Adobe Milo's milo.adobe.com) -- still an SPA, at a lower confidence than same-origin
    # since a cross-origin data call is a slightly weaker signal.
    ratio, in_main, same_origin, cross_data = _injection(ctx)
    if in_main and same_origin == 0 and cross_data >= 1 and ratio >= _SPA_MAIN_RATIO:
        return Hit(0.7, f"main content composed from {cross_data} cross-origin data endpoint(s)", cross_data)
    return None


# -- shadow_dom / iframe: content a plain HTML snapshot would miss ------------
# A shadow root or a same-origin iframe hides its content from page.content() (and so
# from the skeleton). The browser render inlines it (see live.py __wc_inline) and reports
# the counts in render_stats; these flags surface that so the pipeline renders + reads it.

def _shadow_value(signals: "list[Signal]", ctx: Context) -> "int | None":
    """The number of shadow roots the render inlined (the shadow_dom flag's payload), or ``None``."""
    return int((ctx.render_stats or {}).get("shadow", 0) or 0) or None


def _iframe_value(signals: "list[Signal]", ctx: Context) -> "int | None":
    """The iframe count behind the flag -- inlined frames from the render, else counted from the
    tree / raw HTML; ``None`` when there are none."""
    n = int((ctx.render_stats or {}).get("frames", 0) or 0)
    if not n:
        n = len(ctx.tree.cssselect("iframe")) if ctx.tree is not None else ctx.low.count("<iframe")
    return n or None


flag("shadow_dom", value=_shadow_value)
flag("iframe", value=_iframe_value)


@detector(flag="shadow_dom", name="shadow_roots_inlined", stage="rendered")
def _shadow_inlined(ctx: Context) -> Hit | None:
    """shadow_dom evidence (rendered): the render inlined one or more shadow roots that a plain
    HTML snapshot would miss."""
    n = int((ctx.render_stats or {}).get("shadow", 0) or 0)
    if n > 0:
        return Hit(0.9, f"{n} shadow root(s) inlined from the live page", n)
    return None


#: shadow DOM used for REAL -- a call ``el.attachShadow(...)`` or a declarative
#: ``<template shadowrootmode=...>`` -- not a bare mention of the word in prose/JSON.
_SHADOW_RE = re.compile(r"\.attachShadow\s*\(|shadowrootmode\s*=|<template[^>]*\bshadowroot", re.I)


@detector(flag="shadow_dom", name="attach_shadow_marker", stage="static")
def _attach_shadow(ctx: Context) -> Hit | None:
    """shadow_dom evidence (static): a real ``attachShadow()`` call or declarative
    ``shadowrootmode`` template in the HTML -- not a bare mention of the word."""
    if _SHADOW_RE.search(ctx.text or ""):  # a real usage, not the word inside an article/blob
        return Hit(0.5, "the page uses shadow DOM (attachShadow() / shadowrootmode)")
    return None


@detector(flag="iframe", name="iframes_inlined", stage="rendered")
def _iframe_inlined(ctx: Context) -> Hit | None:
    """iframe evidence (rendered): the render inlined one or more same-origin iframe bodies."""
    n = int((ctx.render_stats or {}).get("frames", 0) or 0)
    if n > 0:
        return Hit(0.85, f"{n} same-origin iframe(s) inlined from the live page", n)
    return None


@detector(flag="iframe", name="iframe_element", stage="static")
def _iframe_element(ctx: Context) -> Hit | None:
    """iframe evidence (static): iframe element(s) present in the tree, or the raw HTML in a
    treeless (remote / no-lxml) context."""
    if ctx.tree is not None:
        if fr := ctx.tree.cssselect("iframe"):
            return Hit(0.6, f"{len(fr)} iframe element(s) on the page", len(fr))
        return None
    if "<iframe" in ctx.low:  # treeless (remote / no-lxml) context -- read the raw HTML
        return Hit(0.6, "an iframe element on the page")
    return None


# -- pagination (tree) --------------------------------------------------------


_PAGE_OF = re.compile(r"\bpage\s+\d+\s+of\s+([\d,]+)", re.I)  # "Page 1 of 18"
_SHOWING = re.compile(r"\b([\d,]+)\s*(?:[-–—]|to)\s*([\d,]+)\s+of\s+([\d,]+)", re.I)  # "1-20 of 348"


def _int(s: str) -> int:
    """A comma-grouped integer string as an int (``"1,234" -> 1234``); 0 on garbage."""
    try:
        return int(s.replace(",", ""))
    except ValueError:
        return 0


def _totals(ctx: Context) -> "tuple[int, int, int]":
    """``(total_pages, total_items, page_size)`` read from an ``X-Total-Count`` header and a
    "Page 1 of 18" / "Showing 1-20 of 348" caption -- each 0 when not found."""
    total_pages = total_items = page_size = 0
    xtc = ctx.headers.get("x-total-count", "")
    if xtc.isdigit():
        total_items = int(xtc)
    text = (ctx.visible or (norm(" ".join(ctx.tree.itertext())) if ctx.tree is not None else ""))[:5000]
    if text:
        if m := _PAGE_OF.search(text):
            total_pages = _int(m.group(1))
        if m := _SHOWING.search(text):
            lo, hi, tot = _int(m.group(1)), _int(m.group(2)), _int(m.group(3))
            total_items = total_items or tot
            if 0 < lo <= hi:
                page_size = hi - lo + 1
    return total_pages, total_items, page_size


def _code_next(selector: str, attr: str = "href") -> str:
    """The ``.paginate(next=...)`` that follows ``selector``'s ``attr``."""
    return f".paginate(next=wq.doc.select({json.dumps(selector)}).attr({json.dumps(attr)}))"


#: keys whose value is a KEYSET / continuation cursor (the token carried into the next request).
_CURSOR_KEY = re.compile(
    r"^(end_?cursor|next_?cursor|next_?page_?token|next_?token|continuation(_?token)?|"
    r"page_?token|next_?page|after|cursor|next)$", re.I)
#: keys that a JSON API uses to say a further page EXISTS (a truthy value confirms the cursor).
_HAS_NEXT_KEY = re.compile(r"^(has_?next(_?page)?|has_?more(_?(items|results|pages))?|more(_?results)?)$", re.I)


def _json_body(ctx: Context) -> Any:
    """The response parsed as JSON, or ``None`` -- for a native JSON API response (an XHR/data
    endpoint), NOT an HTML page. Cheap and total: a non-JSON or oversized body just yields ``None``."""
    ct = ctx.headers.get("content-type", "")
    head = ctx.text.lstrip()[:1]
    if ctx.is_html or (("json" not in ct) and head not in ("{", "[")):
        return None
    try:
        return json.loads(ctx.text)
    except (ValueError, TypeError, RecursionError):
        return None


def _longest_list(obj: Any, path: str = "", best: "tuple[int, str] | None" = None) -> "tuple[int, str] | None":
    """``(len, dotted-path)`` of the longest LIST-OF-OBJECTS anywhere in ``obj`` -- the records list a
    keyset API pages through (``items`` / ``data`` / ``results`` / ``edges``). ``None`` when none."""
    if isinstance(obj, list):
        if obj and sum(isinstance(x, dict) for x in obj) >= max(1, len(obj) // 2):
            if best is None or len(obj) > best[0]:
                best = (len(obj), path)
        for i, x in enumerate(obj[:3]):  # a records list may be nested under a wrapper object
            best = _longest_list(x, f"{path}.{i}" if path else str(i), best)
    elif isinstance(obj, dict):
        for k, v in obj.items():
            best = _longest_list(v, f"{path}.{k}" if path else str(k), best)
    return best


def _find_cursor(obj: Any, path: str = "", depth: int = 0) -> "tuple[str, str] | None":
    """``(dotted-path, key)`` of the first KEYSET cursor token in ``obj``: a cursor-named key
    (``endCursor`` / ``next_cursor`` / ``nextPageToken`` / ``after``) whose value is a NON-EMPTY
    scalar (a real token, not ``null`` = the last page). Cursors live in the paging OBJECT, so this
    walks dicts (not the records list) to a shallow depth. ``None`` when no live cursor is present."""
    if depth > 4 or not isinstance(obj, dict):
        return None
    for k, v in obj.items():
        if _CURSOR_KEY.match(str(k)) and isinstance(v, (str, int)) and not isinstance(v, bool) and str(v).strip():
            s = str(v).strip()
            if s.startswith(("http://", "https://", "//", "/")) or "://" in s:
                continue  # a URL is a next-LINK (follow it), not a keyset token carried in a param
            return (f"{path}.{k}" if path else str(k)), str(k)
    for k, v in obj.items():  # descend into nested paging objects (pageInfo / meta / paging)
        if isinstance(v, dict) and (hit := _find_cursor(v, f"{path}.{k}" if path else str(k), depth + 1)):
            return hit
    return None


def _has_next(obj: Any, depth: int = 0) -> bool:
    """Whether a ``hasNextPage`` / ``has_more`` style boolean anywhere in the paging object is truthy."""
    if depth > 4 or not isinstance(obj, dict):
        return False
    for k, v in obj.items():
        if _HAS_NEXT_KEY.match(str(k)) and v is True:
            return True
    return any(_has_next(v, depth + 1) for v in obj.values() if isinstance(v, dict))


@detector(flag="pagination", name="json_cursor", stage="static")
def _json_cursor(ctx: Context) -> "Hit | None":
    """pagination evidence: a native JSON response that is KEYSET (cursor) paginated -- a records list
    plus a live continuation token (``pageInfo.endCursor`` + ``hasNextPage``, ``next_cursor``,
    ``nextPageToken``). The value is the token's dotted PATH + key; ``_pagination_value`` turns it into
    ``cursor=`` pager modes with candidate request params (the response names the token, not the param
    it rides in), which the authoring probe then confirms by walking to a distinct second page."""
    body = _json_body(ctx)
    if body is None:
        return None
    records = _longest_list(body)
    cursor = _find_cursor(body) if isinstance(body, dict) else None
    if not records or records[0] < 1 or cursor is None:
        return None
    path, key = cursor
    conf = 0.7 if _has_next(body) else 0.6
    return Hit(conf, f"a JSON keyset cursor ({path})", {"path": path, "key": key, "records": records[1]})


def _cursor_params(key: str) -> "list[str]":
    """Candidate REQUEST params a keyset token rides in, best-first -- the response names the TOKEN
    (``endCursor``) but not the URL param (``?after=``), so we emit a few by convention and let the
    authoring probe keep the one that actually advances a page. Covers the real conventions: Relay
    ``endCursor`` -> ``after``; Google ``nextPageToken`` -> ``pageToken``; Slack ``next_cursor`` ->
    ``cursor``; Notion ``next_cursor`` -> ``start_cursor``; X ``next_token`` -> ``pagination_token``."""
    k = key.lower()
    if "endcursor" in k:                       # Relay/GraphQL: the param is `after`, never `endCursor`
        order = ["after", "cursor", "start_cursor"]
    elif "token" in k:                         # Google `pageToken`, X `pagination_token`, else the key
        order = ["pageToken", "page_token", "pagination_token", key]
    elif "cursor" in k:                        # Slack `cursor`, Notion `start_cursor`, or the key itself
        order = [key, "cursor", "after", "start_cursor"]
    elif k in ("next", "nextpage"):
        order = ["cursor", "after", "next"]
    elif k == "after":
        order = ["after", "cursor"]
    else:
        order = [key, "after", "cursor"]
    out: list[str] = []
    for p in order:
        if p not in out:
            out.append(p)
    return out[:4]  # the probe stops at the first that advances; extra candidates only cost a probe fetch


def _pagination_value(signals: "list[Signal]", ctx: Context) -> Any:
    """A :class:`PaginationHint`: every WAY the page could be paged that the signals saw, as a
    ranked list of :class:`PagerHint` modes -- each with the ``.paginate(...)`` to write and its
    evidence -- plus the totals a caption / header reveals. Hints only: nothing here runs a walk.
    ``None`` when nothing fired."""
    from ..core.document.models import PagerHint, PaginationHint

    if not signals:
        return None
    by = {s.name: s for s in signals}
    total_pages, total_items, page_size = _totals(ctx)
    modes: list[PagerHint] = []
    if "link_header_next" in by:
        modes.append(PagerHint(mode="next", via="header", code=".paginate(next=wq.doc.next_link())",
                               evidence="an HTTP Link rel=next header", confidence=0.95))
    if "rel_next_link" in by and not any(m.via == "header" for m in modes):
        sel = str(by["rel_next_link"].value or 'a[rel="next"]')  # next_link() reads rel=next (and the header)
        modes.append(PagerHint(mode="next", selector=sel, attr="href", code=".paginate(next=wq.doc.next_link())",
                               evidence="a rel=next link", confidence=0.9))
    if "page_param_links" in by and isinstance(by["page_param_links"].value, dict):
        v = by["page_param_links"].value
        param, start, step = str(v["param"]), int(v["start"]), max(1, int(v["step"]))
        stop = 0
        if v.get("offset"):
            if total_items:
                stop = ((total_items - 1) // step) * step  # the last page's offset
        elif total_pages:
            stop = total_pages
        elif total_items and page_size:
            stop = -(-total_items // page_size)
        args = f'pages="{param}", start={start}, step={step}' + (f", stop={stop}" if stop else "")
        modes.append(PagerHint(mode="pages", param=param, start=start, step=step, stop=stop,
                               code=f".paginate({args})", confidence=0.75,
                               evidence=f"links carry ?{param}= (this page {start}, the next {start + step})"))
    if "next_text_link" in by:
        sel = str(by["next_text_link"].value)
        if not any(m.mode == "next" and m.selector == sel for m in modes):
            modes.append(PagerHint(mode="next", selector=sel, attr="href", code=_code_next(sel),
                                   evidence="a link labelled next", confidence=0.65))
    if "load_more_control" in by:
        sel = str(by["load_more_control"].value)
        modes.append(PagerHint(mode="click", selector=sel, browser=True, confidence=0.55,
                               code=f".paginate(click={json.dumps(sel)})", evidence="a load-more control (needs the page in a browser)"))
    if "json_cursor" in by and isinstance(by["json_cursor"].value, dict):
        v = by["json_cursor"].value
        path, key = str(v["path"]), str(v["key"])
        base = by["json_cursor"].confidence  # candidate request params, best-first (the probe confirms one)
        for i, param in enumerate(_cursor_params(key)):
            code = f'.paginate(cursor=wq.doc.select({json.dumps(path)}), param={json.dumps(param)})'
            modes.append(PagerHint(mode="cursor", selector=path, param=param, code=code,
                                   evidence=f"a JSON keyset cursor ({path}) carried in ?{param}=",
                                   confidence=round(base - i * 0.05, 3)))
    modes.sort(key=lambda m: -m.confidence)
    return PaginationHint(modes=modes, total_pages=total_pages, total_items=total_items, page_size=page_size)


flag("pagination", value=_pagination_value)


_NEXTISH = re.compile(r"^\s*(next|next\s*page|older|older posts|more results|›|»|>|→)\s*[›»>→]?\s*$", re.I)
_MOREISH = re.compile(r"^\s*(load|show|see|view)\s+more\b|^\s*more\s*(results|items|products)?\s*$", re.I)


def _css_for(el: Any, root: Any) -> str:
    """A short CSS selector that picks ``el`` on the page: its id, else its tag + classes, else scoped
    under its parent's, else its aria-label -- the first that matches ONE element ("" when none does)."""
    def own(e: Any) -> str:
        if e.get("id"):
            return f"#{e.get('id')}"
        cls = [c for c in (e.get("class") or "").split() if c and not re.match(r"^(js-|is-|has-)", c)][:2]
        return str(e.tag) + "".join(f".{c}" for c in cls)
    mine = own(el)
    cand = [mine] if mine != el.tag else []  # a bare tag is unique only by luck: scope it first
    parent = el.getparent()
    if parent is not None and parent.tag not in ("html", "body"):
        cand.append(f"{own(parent)} {mine}")
        if el.tag == "a" and el.get("rel"):
            cand.append(f'{own(parent)} a[rel="{el.get("rel")}"]')
    if el.get("aria-label"):
        cand.append(f'{el.tag}[aria-label="{el.get("aria-label")}"]')
    cand.append(mine)
    for c in cand:
        try:
            if len(root.cssselect(c)) == 1:
                return c
        except Exception:  # noqa: BLE001 - a class that is not valid CSS
            continue
    return ""  # nothing picks it alone: no hint beats a wrong one


@detector(flag="pagination", name="rel_next_link", stage="static")
def _rel_next(ctx: Context) -> Hit | None:
    """pagination evidence (strong): a ``rel="next"`` link/anchor -- the canonical next-page marker.
    Its selector is the value (``a[rel="next"]``, or a ``<link>`` in the head)."""
    if ctx.tree is None:
        return None
    if ctx.tree.cssselect('a[rel="next"]'):
        return Hit(0.9, "a rel=next link", 'a[rel="next"]')
    if ctx.tree.cssselect('link[rel="next"]'):
        return Hit(0.9, "a rel=next link", 'link[rel="next"]')
    return None


@detector(flag="pagination", name="next_text_link", stage="static")
def _next_text_link(ctx: Context) -> Hit | None:
    """pagination evidence: a link LABELLED next (its text or aria-label: "Next", "›", "Older posts") --
    a pager without rel=next. Its selector is the value."""
    if ctx.tree is None:
        return None
    for el in ctx.tree.cssselect("a[href]"):
        label = norm("".join(el.itertext())) or (el.get("aria-label") or el.get("title") or "")
        if el.get("rel") == "next" or not (label and _NEXTISH.match(label)) or (el.get("href") or "").strip() in ("", "#"):
            continue  # rel=next is its own (stronger) signal
        sel = _css_for(el, ctx.tree)
        if sel:
            return Hit(0.6, f"a link labelled {label!r}", sel)
    return None


@detector(flag="pagination", name="load_more_control", stage="static")
def _load_more_control(ctx: Context) -> Hit | None:
    """pagination evidence: a LOAD MORE control ("Load more", "Show more results") -- a pager that
    appends on click. Its selector is the value."""
    if ctx.tree is None:
        return None
    for el in ctx.tree.cssselect('button, a, [role="button"]'):
        label = norm("".join(el.itertext())) or (el.get("aria-label") or "")
        if label and _MOREISH.match(label) and (sel := _css_for(el, ctx.tree)):
            return Hit(0.55, f"a {label!r} control", sel)
    return None


@detector(flag="pagination", name="pagination_ui", stage="static")
def _pagination_ui(ctx: Context) -> Hit | None:
    """pagination evidence: a pagination/pager widget (by class or aria-label)."""
    if ctx.tree is not None and ctx.tree.cssselect(
        '.pagination, [class*="pagination"], [class*="pager"], [aria-label*="agination"]'
    ):
        return Hit(0.6, "a pagination widget")
    return None


@detector(flag="pagination", name="page_param_links", stage="static")
def _page_param_links(ctx: Context) -> Hit | None:
    """pagination evidence: links whose query carries a pagination param (``?page=``, ``?offset=``, …;
    ``p`` excluded -- too often a post id). The value says how to walk it: the ``param``, this page's
    value (``start``: its URL's, else 1 -- or 0 for an offset) and the ``step`` to the NEXT page's value
    (the smallest one after it among the links) -- so an ``?offset=20`` link walks by 20, and a walk
    started on page 3 goes on to 4. Reads the same param table crawl uses (``crawl.canon``)."""
    if ctx.tree is None:
        return None
    from ..core.crawl.canon import _OFFSET_PARAMS, _PAGINATION_PARAMS

    here = dict(parse_qsl(urlparse(ctx.final_url or ctx.url).query))
    found: dict[str, list[int]] = {}
    for el in ctx.tree.cssselect("a[href]"):
        for k, v in parse_qsl(urlparse(el.get("href") or "").query):
            if k.lower() in _PAGINATION_PARAMS and v.isdigit():
                found.setdefault(k, []).append(int(v))
    for param, values in found.items():
        offset = param.lower() in _OFFSET_PARAMS
        cur = here.get(param)
        start = int(cur) if cur and cur.isdigit() else (0 if offset else 1)
        ahead = sorted(v for v in set(values) if v > start)
        if not ahead:
            continue
        return Hit(0.5, f"links with ?{param}=", {"param": param, "start": start, "step": ahead[0] - start, "offset": offset})
    return None


@detector(flag="pagination", name="numbered_sequence", stage="static")
def _numbered_sequence(ctx: Context) -> Hit | None:
    """pagination evidence: three or more purely-numeric links -- a ``1 2 3`` page-number strip."""
    if ctx.tree is None:
        return None
    nums = [t for el in ctx.tree.cssselect("a[href]") if (t := norm("".join(el.itertext()))).isdigit()]
    return Hit(0.6, "a numbered page sequence") if len(nums) >= 3 else None


# -- ordered (tree + the request params): HOW the listing is sorted -----------
# decides whether an early pagination stop is sound (newest-first dates -> yes; relevance
# / unknown -> the walk must exhaust). Evidence: a sort control, a search box, and
# record dates that run monotonically. The request-stage sort/relevance PARAM detectors
# live in :mod:`.request_static`; these are the tree-based ones + the value reducer.

_ISO_DATE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")  # a sortable ISO date in text


def _page_dates(ctx: Context) -> "list[str]":
    """Sortable date strings in DOM order: HTML5 ``<time datetime>`` attributes first (the clean,
    standard signal), else ISO dates found in the visible text. Truncated to the date part so they
    sort lexically."""
    if ctx.tree is None:
        return []
    stamps = [dt[:10] for el in ctx.tree.cssselect("time[datetime]") if (dt := (el.get("datetime") or "").strip())]
    if len(stamps) >= 3:
        return stamps
    return _ISO_DATE.findall(ctx.visible or "")


def _monotone_direction(vals: "list[str]") -> str:
    """``"desc"`` when the values run non-increasing (newest-first), ``"asc"`` non-decreasing, else
    ``""`` (not monotone, or all equal -- uninformative). Needs at least three values."""
    seq = [v for v in vals if v]
    if len(seq) < 3:
        return ""
    asc = all(a <= b for a, b in zip(seq, seq[1:]))
    desc = all(a >= b for a, b in zip(seq, seq[1:]))
    return "desc" if desc and not asc else "asc" if asc and not desc else ""


@detector(flag="ordered", name="sort_control", stage="static")
def _sort_control(ctx: Context) -> Hit | None:
    """ordered evidence: a sort control on the page (a ``select[name*=sort]`` / ``[aria-sort]`` /
    an order dropdown) -> the listing's order is CONTROLLABLE."""
    if ctx.tree is not None and ctx.tree.cssselect(
        'select[name*="sort"], select[name*="order"], [aria-sort], [class*="sort-by"], [class*="sortby"]'
    ):
        return Hit(0.6, "a sort control")
    return None


@detector(flag="ordered", name="relevance_searchbox", stage="static")
def _relevance_searchbox(ctx: Context) -> Hit | None:
    """ordered evidence: a search box (``input[type=search]`` / ``[role=search]``) -> the listing
    is likely relevance-ordered, so no early pagination stop is sound."""
    if ctx.tree is not None and ctx.tree.cssselect('input[type="search"], [role="search"]'):
        return Hit(0.5, "a search box (relevance order)")
    return None


@detector(flag="ordered", name="monotone_dates", stage="static")
def _monotone_dates(ctx: Context) -> Hit | None:
    """ordered evidence (strong): the page's record dates run MONOTONICALLY -- so the listing is
    date-sorted, and the direction says whether a recency ``until`` stop is sound. The direction
    (``"desc"``/``"asc"``) is the signal value."""
    direction = _monotone_direction(_page_dates(ctx))
    if direction:
        return Hit(0.7, f"record dates run {'newest' if direction == 'desc' else 'oldest'}-first", direction)
    return None


def _ordering_value(signals: "list[Signal]", ctx: Context) -> Any:
    """An :class:`Ordering` (key / direction / controllable / param) built from the ordered signals,
    so a caller knows if an early pagination stop is sound. ``None`` when nothing fired."""
    from ..core.document.models import Ordering

    if not signals:
        return None
    fired = {s.name for s in signals}
    param = next((s.value for s in signals if s.name == "sort_param" and isinstance(s.value, str)), "")
    raw_dir = next((s.value for s in signals if s.name == "monotone_dates" and isinstance(s.value, str)), "")
    direction: Literal["asc", "desc", "unknown"] = "desc" if raw_dir == "desc" else "asc" if raw_dir == "asc" else "unknown"
    key: Literal["date", "alpha", "price", "relevance", "unknown"]
    if raw_dir:
        key = "date"
    elif "relevance_query" in fired or "relevance_searchbox" in fired:
        key = "relevance"
    else:
        key = "unknown"
    return Ordering(key=key, direction=direction, controllable="sort_param" in fired or "sort_control" in fired, param=param)


flag("ordered", value=_ordering_value)


# -- filtered (tree + request params): the listing is NARROWED -----------------

@detector(flag="filtered", name="facet_controls", stage="static")
def _facet_controls(ctx: Context) -> Hit | None:
    """filtered evidence: filter / facet controls on the page (a ``[class*=facet]`` / ``[class*=filter]``
    block, a checkbox filter form, a ``select[name*=filter]``) -> facets to narrow / partition by. The
    control names (best-effort) are the signal value."""
    if ctx.tree is None:
        return None
    names: list[str] = []
    for el in ctx.tree.cssselect('[class*="facet"], [class*="filter"], select[name*="filter"], form input[type="checkbox"]'):
        label = (el.get("name") or el.get("aria-label") or el.get("id") or "").strip()
        if label and label not in names:
            names.append(label)
    hits = ctx.tree.cssselect('[class*="facet"], [class*="filter"], select[name*="filter"]')
    return Hit(0.5, "filter / facet controls", names[:8]) if hits else None


def _filtering_value(signals: "list[Signal]", ctx: Context) -> Any:
    """A :class:`Filtering` (active filter params + the filter controls) from the filtered signals."""
    from ..core.document.models import Filtering

    if not signals:
        return None
    active = next((s.value for s in signals if s.name == "active_query_filters" and isinstance(s.value, dict)), {})
    controls = next((s.value for s in signals if s.name == "facet_controls" and isinstance(s.value, list)), [])
    return Filtering(active=active, controls=controls)


flag("filtered", value=_filtering_value)


# -- live (tree): the listing CHANGES over time (a feed / newest-first list) ----

@detector(flag="live", name="recent_records", stage="static")
def _recent_records(ctx: Context) -> Hit | None:
    """live evidence: the newest record date on the page is within the last month -> a live/timely
    listing (a feed), which shifts while you page. The newest date is the signal value."""
    dates = _page_dates(ctx)
    if len(dates) < 3:
        return None
    newest = max(dates)
    try:
        age = (datetime.now(timezone.utc).date() - date.fromisoformat(newest[:10])).days
    except ValueError:
        return None
    return Hit(0.7, f"recent records (newest {newest})", newest) if 0 <= age <= 31 else None


def _liveness_value(signals: "list[Signal]", ctx: Context) -> Any:
    """A :class:`Liveness` (newest date, recent, drift risk) from the live signals. ``drift_risk`` is
    set when the recent list is also newest-first -- paging it may duplicate/skip at boundaries."""
    from ..core.document.models import Liveness

    if not signals:
        return None
    newest = next((s.value for s in signals if isinstance(s.value, str) and s.value), "")
    desc = _monotone_direction(_page_dates(ctx)) == "desc"
    return Liveness(newest=newest, recent=True, drift_risk=bool(newest and desc))


flag("live", value=_liveness_value)


# -- tabbed (tree) -- same page, content split behind TAB controls ------------
# distinct from pagination (more of the SAME list, another page): tabs show DIFFERENT
# sections/slices on the one page (Upcoming vs Past events, year tabs, categories).

flag("tabbed")

# treeless (remote / no-lxml / signals-only) fallbacks -- read the raw HTML. Kept
# conservative so a plain <table class="data-table"> never trips it: role="tab" needs a
# closing quote (not "table"), and data-tab\b / nav-tab / tab-pane etc. don't match "table".
_ARIA_TAB_RE = re.compile(r'role=["\']tab(?:list|panel)?["\']', re.I)
_TAB_WIDGET_RE = re.compile(
    r'data-tab\b|data-toggle=["\']tab["\']|nav-tab|tab-pane|class=["\'][^"\']*\btabbed\b|tab-list',
    re.I,
)


@detector(flag="tabbed", name="aria_tabs", stage="static")
def _aria_tabs(ctx: Context) -> Hit | None:
    """tabbed evidence (strong): ARIA tab roles (tablist/tab/tabpanel), from the tree or the raw
    HTML in a treeless context."""
    if ctx.tree is not None:
        if ctx.tree.cssselect('[role="tablist"], [role="tab"], [role="tabpanel"]'):
            return Hit(0.9, "ARIA tab roles (tablist / tab / tabpanel)")
        return None
    if _ARIA_TAB_RE.search(ctx.text or ""):  # treeless context -- read the raw HTML
        return Hit(0.9, "ARIA tab roles (tablist / tab / tabpanel)")
    return None


@detector(flag="tabbed", name="tab_widget", stage="static")
def _tab_widget(ctx: Context) -> Hit | None:
    """tabbed evidence: a tab widget (nav-tabs / tab-pane / data-tab), from the tree or raw HTML.
    Deliberately conservative so a plain ``<table>`` never trips it."""
    # conservative selectors -- avoid bare [class*="tab"] (it matches "table")
    if ctx.tree is not None:
        if ctx.tree.cssselect(
            '[data-tab], [data-toggle="tab"], [class*="nav-tab"], .tab-pane, .tabbed, [class*="tab-list"]'
        ):
            return Hit(0.6, "a tab widget (nav-tabs / tab-pane / data-tab)")
        return None
    if _TAB_WIDGET_RE.search(ctx.text or ""):  # treeless context -- read the raw HTML
        return Hit(0.6, "a tab widget (nav-tabs / tab-pane / data-tab)")
    return None


# -- forms (tree) -------------------------------------------------------------


def _forms_value(signals: "list[Signal]", ctx: Context) -> "list[Form] | None":
    """The forms flag's payload: each ``<form>`` as a ``Form`` (method, resolved action, field
    names). ``None`` in a treeless context or when there are none."""
    if ctx.tree is None:
        return None
    from ..core.document.models import Form

    base = ctx.final_url or ctx.url
    forms = [
        Form(
            method=(el.get("method") or "get").lower(),
            action=urljoin(base, el.get("action")) if el.get("action") else None,
            field_names=sorted({n for f in el.cssselect("input, select, textarea") if (n := f.get("name"))}),
        )
        for el in ctx.tree.cssselect("form")
    ]
    return forms or None


flag("forms", value=_forms_value)


@detector(flag="forms", name="form_element", stage="static")
def _form_element(ctx: Context) -> Hit | None:
    """forms evidence: one or more ``<form>`` elements on the page."""
    if ctx.tree is not None and (forms := ctx.tree.cssselect("form")):
        return Hit(0.9, f"{len(forms)} form(s)")
    return None


# -- buttons (tree) -----------------------------------------------------------


def _buttons_value(signals: "list[Signal]", ctx: Context) -> list[str] | None:
    """The buttons flag's payload: a sample (up to 10) of button labels on the page. ``None`` in a
    treeless context or when there are none."""
    if ctx.tree is None:
        return None
    btns = ctx.tree.cssselect('button, input[type="submit"], input[type="button"]')
    labels = [t for el in btns if (t := norm("".join(el.itertext())) or el.get("value") or "")][:10]
    return labels or None


flag("buttons", value=_buttons_value)


@detector(flag="buttons", name="button_element", stage="static")
def _button_element(ctx: Context) -> Hit | None:
    """buttons evidence: ``<button>`` / submit / button inputs on the page."""
    if ctx.tree is not None and (btns := ctx.tree.cssselect('button, input[type="submit"], input[type="button"]')):
        return Hit(0.9, f"{len(btns)} button(s)")
    return None


@detector(flag="buttons", name="role_button", stage="static")
def _role_button(ctx: Context) -> Hit | None:
    """buttons evidence: ``role="button"`` elements (buttons that aren't ``<button>`` tags)."""
    if ctx.tree is not None and ctx.tree.cssselect('[role="button"]'):
        return Hit(0.6, "role=button elements")
    return None


@detector(flag="buttons", name="onclick_attr", stage="static")
def _onclick_attr(ctx: Context) -> Hit | None:
    """buttons evidence (weak): elements carrying an inline ``onclick`` handler."""
    if ctx.tree is not None and ctx.tree.cssselect("[onclick]"):
        return Hit(0.4, "onclick handlers")
    return None
