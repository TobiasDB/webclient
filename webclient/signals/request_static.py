"""Request + static detectors -- pure (regex/text, no lxml), so they run on a remote
resolve too and ``auto`` can read them on the first hop. Each is a small registered
function; add one to extend detection.

Covers the flags ``auto`` acts on: spa (static evidence), anti_bot_present /
anti_bot_triggered, login_present / login_required. The rendered/network and
tree-based signals live in :mod:`.dom` (facet-only).
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from .context import Context
from .registry import Hit, detector, flag

if TYPE_CHECKING:
    from ..core.document.models import Signal

_SPA_SHELL = re.compile(
    r'<(?:div|main|app-root|#document)[^>]*\bid=["\'](?:root|app|__next|__nuxt)["\']'
    r'[^>]*>\s*</', re.I,
)
_SIGNIN = re.compile(r"\b(sign[\s-]?in|log[\s-]?in|login)\b", re.I)

#: (framework name, a marker substring in the served HTML)
_FRAMEWORKS = (
    ("next", "__NEXT_DATA__"), ("next", "/_next/"),
    ("nuxt", "__NUXT__"), ("nuxt", "/_nuxt/"),
    ("react", "data-reactroot"), ("react", "react-dom"),
    ("angular", "ng-version"), ("vue", "data-v-"), ("svelte", "svelte-"),
    ("gatsby", "___gatsby"), ("remix", "__remixContext"), ("astro", "astro-island"),
    ("aem-edge", "window.hlx"), ("aem-edge", "/scripts/aem.js"),
    # content-as-a-service: a client widget fetches the records from a content API, so the
    # served HTML is only a shell -- auto must render it to see the data (e.g. Adobe Milo).
    ("aem-milo", "milo.adobe.com"), ("caas", "/tools/caas"), ("caas", "data-caas"),
    ("contentful", "cdn.contentful.com"), ("sanity", ".sanity.io"),
)
_HYDRATION_BLOBS = (
    "__INITIAL_STATE__", "__APOLLO_STATE__", "__PRELOADED_STATE__",
    "data-server-rendered", 'id="__next"', 'id="__nuxt"',
)
#: (vendor, header substrings, cookie-name substrings, strong body substrings)
_ANTIBOT: tuple[tuple[str, tuple[str, ...], tuple[str, ...], tuple[str, ...]], ...] = (
    ("cloudflare", ("cf-ray", "cf-mitigated"), ("__cf_bm", "cf_clearance"),
     ("just a moment", "checking your browser", "cf-browser-verification")),
    ("datadome", ("x-datadome", "x-dd-"), ("datadome",), ("datadome",)),
    ("perimeterx", (), ("_px", "_pxhd", "_pxappid"), ("px-captcha", "perimeterx")),
    ("akamai", ("akamai-grn",), ("_abck", "ak_bmsc"), ("akamaighost",)),
    ("kasada", ("x-kpsdk",), (), ("kasada",)),
    ("awswaf", ("x-amzn-waf",), ("aws-waf-token",), ()),
    ("incapsula", ("x-iinfo",), ("visid_incap", "incap_ses"), ("incapsula incident",)),
)
_CHALLENGE_STATUS = frozenset({403, 429, 503})


def _framework(html: str) -> str | None:
    for name, marker in _FRAMEWORKS:
        if marker in html:
            return name
    return None


def _vendor_fingerprint(ctx: Context) -> str | None:
    """A vendor whose header/cookie is present on ANY status (in front of the site)."""
    for vendor, weak_hdrs, weak_cooks, _ in _ANTIBOT:
        if any(s in ctx.header_blob for s in weak_hdrs) or any(c in ctx.cookie_blob for c in weak_cooks):
            return vendor
    return None


def _vendor_interstitial(low: str) -> str | None:
    for vendor, _, _, strong_body in _ANTIBOT:
        if any(m in low for m in strong_body):
            return vendor
    return None


# -- flags --------------------------------------------------------------------


def _spa_endpoints(signals: "list[Signal]", ctx: Context) -> list[str] | None:
    """Same-origin XHR/fetch endpoints, duck-typed off the events so this stays pure.
    Empty on a request/static context; filled by the facet's browser events."""
    eps: list[str] = []
    for e in ctx.events:
        if getattr(e, "resource_type", None) in ("xhr", "fetch"):
            req = getattr(e, "request", None)
            if req is not None:
                try:
                    eps.append(str(req.dispatch("url")))
                except Exception:
                    pass
    return eps or None


def _antibot_remedy(signals: "list[Signal]", ctx: Context) -> str | None:
    # a named vendor -> a stealth browser; a bare block -> a fresh proxy exit.
    named = any(s.name in ("challenge_interstitial", "vendor_on_block") for s in signals)
    return "stealth" if named else "proxy"


def _antibot_value(signals: "list[Signal]", ctx: Context) -> str | None:
    return next((s.value for s in signals if isinstance(s.value, str)), None)


#: decoded-body size (chars) at/above which a page is "large" -- the DOM (and any skeleton
#: derived from it) is big enough that a skeleton view will be trimmed/collapsed to fit a budget.
#: This flag is a property of the CONTENT SIZE alone, independent of any skeleton rendering.
_LARGE_DOC_CHARS = 300_000


def _large_doc_value(signals: "list[Signal]", ctx: Context) -> "int | None":
    """The decoded-body size behind the flag (chars), so a caller sees HOW large."""
    return next((s.value for s in signals if isinstance(s.value, int)), None)


flag("spa", remedy="browser", value=_spa_endpoints)
flag("anti_bot_present", value=_antibot_value)
flag("anti_bot_triggered", remedy=_antibot_remedy, value=_antibot_value)
flag("login_present")
flag("login_required")
flag("large_document", value=_large_doc_value)


@detector(flag="large_document", name="large_body", stage="static")
def _large_body(ctx: Context) -> Hit | None:
    """A LARGE document -- the decoded body is big enough that a skeleton of it will be
    trimmed/collapsed to fit a token budget, so a query author sees only a reduced view. Based on
    CONTENT SIZE, not on the skeleton. Confidence grows with size."""
    n = len(ctx.text)
    if n < _LARGE_DOC_CHARS:
        return None
    conf = min(0.95, 0.6 + (n - _LARGE_DOC_CHARS) / 2_000_000)
    return Hit(round(conf, 2), f"{n:,} chars of content (a skeleton of it will be trimmed)", n)


# -- spa (static evidence) ----------------------------------------------------


@detector(flag="spa", name="framework_marker", stage="static")
def _framework_marker(ctx: Context) -> Hit | None:
    fw = _framework(ctx.text)
    return Hit(0.6, f"a {fw} client-render marker", fw) if fw else None


@detector(flag="spa", name="empty_root_shell", stage="static")
def _empty_root_shell(ctx: Context) -> Hit | None:
    return Hit(0.9, "an empty hydration root a bundle populates") if _SPA_SHELL.search(ctx.text) else None


@detector(flag="spa", name="empty_body_scripted", stage="static")
def _empty_body_scripted(ctx: Context) -> Hit | None:
    if ctx.is_html and ctx.status == 200 and len(ctx.visible) < 40 and "<script" in ctx.low:
        return Hit(0.7, "a near-empty body with a script")
    return None


@detector(flag="spa", name="hydration_state_blob", stage="static")
def _hydration_state_blob(ctx: Context) -> Hit | None:
    return Hit(0.5, "a serialized initial-state blob") if any(m in ctx.text for m in _HYDRATION_BLOBS) else None


@detector(flag="spa", name="static_content_present", stage="static", contra=True)
def _static_content_present(ctx: Context) -> Hit | None:
    # CONTRA evidence: a big, already-populated static body argues AGAINST a client-rendered
    # shell -- even a framework-marked page that server-renders its content is scrapeable
    # without JS, so we shouldn't escalate it to a browser on a lone framework marker.
    if ctx.is_html and len(ctx.visible) > 2000:
        return Hit(0.6, f"{len(ctx.visible)} chars of content already in the served HTML")
    return None


# -- anti-bot -----------------------------------------------------------------


@detector(flag="anti_bot_present", name="vendor_fingerprint", stage="request")
def _fingerprint(ctx: Context) -> Hit | None:
    vendor = _vendor_fingerprint(ctx)
    return Hit(0.5, f"a {vendor} header/cookie", vendor) if vendor else None


@detector(flag="anti_bot_triggered", name="challenge_interstitial", stage="static")
def _interstitial(ctx: Context) -> Hit | None:
    vendor = _vendor_interstitial(ctx.low)
    return Hit(0.95, f"a {vendor} challenge page", vendor) if vendor else None


@detector(flag="anti_bot_triggered", name="challenge_status", stage="request")
def _challenge_status(ctx: Context) -> Hit | None:
    return Hit(0.6, f"a challenge status ({ctx.status})", ctx.status) if ctx.status in _CHALLENGE_STATUS else None


@detector(flag="anti_bot_triggered", name="vendor_on_block", stage="request")
def _vendor_on_block(ctx: Context) -> Hit | None:
    if ctx.status in _CHALLENGE_STATUS and (vendor := _vendor_fingerprint(ctx)):
        return Hit(0.9, f"{vendor} on a blocking status", vendor)
    return None


# -- login --------------------------------------------------------------------


def _has_password(low: str) -> bool:
    return 'type="password"' in low or "type='password'" in low


@detector(flag="login_present", name="password_field", stage="static")
def _password_field(ctx: Context) -> Hit | None:
    return Hit(0.6, "a password field") if _has_password(ctx.low) else None


@detector(flag="login_present", name="signin_link", stage="static")
def _signin_link(ctx: Context) -> Hit | None:
    return Hit(0.35, "a sign-in link/label") if _SIGNIN.search(ctx.low) else None


@detector(flag="login_required", name="status_401", stage="request")
def _status_401(ctx: Context) -> Hit | None:
    return Hit(0.95, "a 401 unauthorized", 401) if ctx.status == 401 else None


@detector(flag="login_required", name="password_on_sparse_page", stage="static")
def _password_on_sparse_page(ctx: Context) -> Hit | None:
    if _has_password(ctx.low) and ctx.is_html and len(ctx.visible) < 500:
        return Hit(0.7, "a password field on a page with little other content")
    return None


@detector(flag="login_required", name="redirect_to_login", stage="request")
def _redirect_to_login(ctx: Context) -> Hit | None:
    if len(ctx.redirect_chain) > 1 and any(
        p in ctx.redirect_chain[-1] for p in ("/login", "/signin", "/sign-in", "/auth")
    ):
        return Hit(0.8, "redirected to a login URL")
    return None
