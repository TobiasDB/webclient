"""Facet-only detectors: the rendered/network SPA signals and the tree-based flags
(pagination / forms / buttons). Imported by the ``flags`` facet, which fills the
``Context``'s ``tree`` / ``events`` / ``render_stats``; each detector self-skips
(returns ``None``) when its inputs are absent, so a request/static context is safe.

No lxml at import time -- the tree arrives on the context and ``cssselect`` is a
method on it -- so importing this module stays remote-safe.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from urllib.parse import urljoin, urlparse

from .context import Context, norm
from .registry import Hit, detector, flag

if TYPE_CHECKING:
    from ..core.document.models import Form, Signal

_SPA_RATIO = 0.4  # injected-text share that alone marks a SPA
_SPA_MAIN_RATIO = 0.15  # lower bar when the injection is main-area + same-origin XHR


def _xhr_events(ctx: Context) -> list[Any]:
    return [e for e in ctx.events if getattr(e, "resource_type", None) in ("xhr", "fetch")]


def _injection(ctx: Context) -> "tuple[float, bool, int]":
    """``(injected_ratio, injected_in_main, same_origin_xhr_count)`` from the render.
    ``injected_ratio`` = NET text grown past the DOMContentLoaded baseline / final
    text. All zero without a browser render."""
    mutations = [e for e in ctx.events if getattr(e, "kind", None) is not None and hasattr(e, "detail")]
    load_added = [
        e for e in mutations
        if getattr(e, "kind", None) == "added" and (getattr(e, "detail", None) or {}).get("phase") == "load"
    ]
    in_main = any((getattr(e, "detail", None) or {}).get("inMain") for e in load_added)
    page_host = (urlparse(ctx.final_url or ctx.url).hostname or "").lower()
    same_origin = 0
    for e in _xhr_events(ctx):
        req = getattr(e, "request", None)
        if req is not None:
            try:
                if (urlparse(str(req.dispatch("url"))).hostname or "").lower() == page_host:
                    same_origin += 1
            except Exception:
                pass
    stats = ctx.render_stats or {}
    total = int(stats.get("text", 0)) or len(norm(ctx.text))
    if stats.get("dclText") is not None and total:
        ratio = round(max(0, total - int(stats["dclText"])) / total, 3)
    else:
        ratio = 0.0
    return ratio, in_main, same_origin


# -- spa: rendered + network evidence (joins the static signals in request_static) --


@detector(flag="spa", name="body_injected", stage="rendered")
def _body_injected(ctx: Context) -> Hit | None:
    ratio, _, _ = _injection(ctx)
    if ratio >= _SPA_RATIO:
        return Hit(0.9, f"{ratio:.0%} of the page's text was injected after the initial response", ratio)
    return None


@detector(flag="spa", name="xhr_composed", stage="network")
def _xhr_composed(ctx: Context) -> Hit | None:
    ratio, in_main, same_origin = _injection(ctx)
    if in_main and same_origin >= 1 and ratio >= _SPA_MAIN_RATIO:
        return Hit(0.95, f"main content composed from {same_origin} same-origin XHR call(s)", same_origin)
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
