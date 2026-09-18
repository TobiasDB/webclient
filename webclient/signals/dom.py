"""Facet-only detectors: the rendered/network SPA signals and the tree-based flags
(pagination / forms / buttons). Imported by the ``flags`` facet, which fills the
``Context``'s ``tree`` / ``events`` / ``render_stats``; each detector self-skips
(returns ``None``) when its inputs are absent, so a request/static context is safe.

No lxml at import time -- the tree arrives on the context and ``cssselect`` is a
method on it -- so importing this module stays remote-safe.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any
from urllib.parse import urljoin, urlparse

from .context import Context, norm
from .registry import Hit, detector, flag

if TYPE_CHECKING:
    from ..core.document.models import Form, Signal

_log = logging.getLogger(__name__)

_SPA_RATIO = 0.4  # injected-text share that alone marks a SPA
_SPA_MAIN_RATIO = 0.15  # lower bar when the injection is main-area + same-origin XHR


def _xhr_events(ctx: Context) -> list[Any]:
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
    ratio, _, _, _ = _injection(ctx)
    if ratio >= _SPA_RATIO:
        return Hit(0.9, f"{ratio:.0%} of the page's text was injected after the initial response", ratio)
    return None


@detector(flag="spa", name="xhr_composed", stage="network")
def _xhr_composed(ctx: Context) -> Hit | None:
    ratio, in_main, same_origin, _ = _injection(ctx)
    if in_main and same_origin >= 1 and ratio >= _SPA_MAIN_RATIO:
        return Hit(0.95, f"main content composed from {same_origin} same-origin XHR call(s)", same_origin)
    return None


@detector(flag="spa", name="xhr_composed_cross_origin", stage="network")
def _xhr_composed_cross_origin(ctx: Context) -> Hit | None:
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
    return int((ctx.render_stats or {}).get("shadow", 0) or 0) or None


def _iframe_value(signals: "list[Signal]", ctx: Context) -> "int | None":
    n = int((ctx.render_stats or {}).get("frames", 0) or 0)
    if not n:
        n = len(ctx.tree.cssselect("iframe")) if ctx.tree is not None else ctx.low.count("<iframe")
    return n or None


flag("shadow_dom", value=_shadow_value)
flag("iframe", value=_iframe_value)


@detector(flag="shadow_dom", name="shadow_roots_inlined", stage="rendered")
def _shadow_inlined(ctx: Context) -> Hit | None:
    n = int((ctx.render_stats or {}).get("shadow", 0) or 0)
    if n > 0:
        return Hit(0.9, f"{n} shadow root(s) inlined from the live page", n)
    return None


#: shadow DOM used for REAL -- a call ``el.attachShadow(...)`` or a declarative
#: ``<template shadowrootmode=...>`` -- not a bare mention of the word in prose/JSON.
_SHADOW_RE = re.compile(r"\.attachShadow\s*\(|shadowrootmode\s*=|<template[^>]*\bshadowroot", re.I)


@detector(flag="shadow_dom", name="attach_shadow_marker", stage="static")
def _attach_shadow(ctx: Context) -> Hit | None:
    if _SHADOW_RE.search(ctx.text or ""):  # a real usage, not the word inside an article/blob
        return Hit(0.5, "the page uses shadow DOM (attachShadow() / shadowrootmode)")
    return None


@detector(flag="iframe", name="iframes_inlined", stage="rendered")
def _iframe_inlined(ctx: Context) -> Hit | None:
    n = int((ctx.render_stats or {}).get("frames", 0) or 0)
    if n > 0:
        return Hit(0.85, f"{n} same-origin iframe(s) inlined from the live page", n)
    return None


@detector(flag="iframe", name="iframe_element", stage="static")
def _iframe_element(ctx: Context) -> Hit | None:
    if ctx.tree is not None:
        if fr := ctx.tree.cssselect("iframe"):
            return Hit(0.6, f"{len(fr)} iframe element(s) on the page", len(fr))
        return None
    if "<iframe" in ctx.low:  # treeless (remote / no-lxml) context -- read the raw HTML
        return Hit(0.6, "an iframe element on the page")
    return None


# -- pagination (tree) --------------------------------------------------------


def _pagination_value(signals: "list[Signal]", ctx: Context) -> Any:
    return next((s.value for s in signals if s.value), None)


flag("pagination", value=_pagination_value)


@detector(flag="pagination", name="rel_next_link", stage="static")
def _rel_next(ctx: Context) -> Hit | None:
    if ctx.tree is not None and ctx.tree.cssselect('a[rel="next"], link[rel="next"]'):
        return Hit(0.9, "a rel=next link")
    return None


@detector(flag="pagination", name="pagination_ui", stage="static")
def _pagination_ui(ctx: Context) -> Hit | None:
    if ctx.tree is not None and ctx.tree.cssselect(
        '.pagination, [class*="pagination"], [class*="pager"], [aria-label*="agination"]'
    ):
        return Hit(0.6, "a pagination widget")
    return None


@detector(flag="pagination", name="page_param_links", stage="static")
def _page_param_links(ctx: Context) -> Hit | None:
    if ctx.tree is None:
        return None
    base = ctx.final_url or ctx.url
    for el in ctx.tree.cssselect("a[href]"):
        h = el.get("href")
        if h and any(p in h for p in ("?page=", "&page=", "?p=", "&p=", "/page/")):
            return Hit(0.5, "links with a page parameter", urljoin(base, h))
    return None


@detector(flag="pagination", name="numbered_sequence", stage="static")
def _numbered_sequence(ctx: Context) -> Hit | None:
    if ctx.tree is None:
        return None
    nums = [t for el in ctx.tree.cssselect("a[href]") if (t := norm("".join(el.itertext()))).isdigit()]
    return Hit(0.6, "a numbered page sequence") if len(nums) >= 3 else None


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
    if ctx.tree is not None:
        if ctx.tree.cssselect('[role="tablist"], [role="tab"], [role="tabpanel"]'):
            return Hit(0.9, "ARIA tab roles (tablist / tab / tabpanel)")
        return None
    if _ARIA_TAB_RE.search(ctx.text or ""):  # treeless context -- read the raw HTML
        return Hit(0.9, "ARIA tab roles (tablist / tab / tabpanel)")
    return None


@detector(flag="tabbed", name="tab_widget", stage="static")
def _tab_widget(ctx: Context) -> Hit | None:
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
    if ctx.tree is not None and (forms := ctx.tree.cssselect("form")):
        return Hit(0.9, f"{len(forms)} form(s)")
    return None


# -- buttons (tree) -----------------------------------------------------------


def _buttons_value(signals: "list[Signal]", ctx: Context) -> list[str] | None:
    if ctx.tree is None:
        return None
    btns = ctx.tree.cssselect('button, input[type="submit"], input[type="button"]')
    labels = [t for el in btns if (t := norm("".join(el.itertext())) or el.get("value") or "")][:10]
    return labels or None


flag("buttons", value=_buttons_value)


@detector(flag="buttons", name="button_element", stage="static")
def _button_element(ctx: Context) -> Hit | None:
    if ctx.tree is not None and (btns := ctx.tree.cssselect('button, input[type="submit"], input[type="button"]')):
        return Hit(0.9, f"{len(btns)} button(s)")
    return None


@detector(flag="buttons", name="role_button", stage="static")
def _role_button(ctx: Context) -> Hit | None:
    if ctx.tree is not None and ctx.tree.cssselect('[role="button"]'):
        return Hit(0.6, "role=button elements")
    return None


@detector(flag="buttons", name="onclick_attr", stage="static")
def _onclick_attr(ctx: Context) -> Hit | None:
    if ctx.tree is not None and ctx.tree.cssselect("[onclick]"):
        return Hit(0.4, "onclick handlers")
    return None
