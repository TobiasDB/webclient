"""Pure request+static detection for the flags layer.

``static_signals(...)`` reads a raw response (status / headers / cookies / body /
redirect chain) and emits every :class:`~webclient.core.document.models.Signal` it
can WITHOUT a browser render -- front-loading detection so ``auto`` and the flags
facet have as much evidence as possible from the first hop. ``static_flags(...)``
rolls those signals up into the request/static :class:`~...models.Flag`\\ s.

**Pure** (no cores, no IO, no lxml) so a local and a remote resolve agree, and
conservative (well-known markers only) so ordinary pages score near zero. The
rendered/network signals (a real SPA's injected content, its XHR endpoints) are
added by the ``flags`` facet, which has the captured DOM/network events.

Anti-bot fingerprints follow the public markers (Cloudflare / DataDome / PerimeterX
/ Akamai / Kasada / AWS WAF / Incapsula). ``Signal``/``Flag`` are imported lazily
(runtime only) to avoid an import cycle with ``core.document``.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from ..core.document.models import Flag, Signal, Stage

_TAGS = re.compile(r"<(script|style)\b[^>]*>.*?</\1>", re.I | re.S)
_ANYTAG = re.compile(r"<[^>]+>")
_SPA_SHELL = re.compile(
    r'<(?:div|main|app-root|#document)[^>]*\bid=["\'](?:root|app|__next|__nuxt)["\']'
    r'[^>]*>\s*</',
    re.I,
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
)
#: serialized initial-state blobs a hydrating client leaves in the HTML.
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

#: statuses that are, on their own, a bot block / challenge (a bare 403/429/503 --
#: an origin WAF or rate-limit gate). 401 stays out: that is an auth/login wall.
_CHALLENGE_STATUS = frozenset({403, 429, 503})
_PRESENT = 0.5  # a flag's confidence must reach this to be "present"


def _lower_map(headers: "Mapping[Any, Any] | Iterable[tuple[Any, Any]]") -> dict[str, str]:
    items = headers.items() if isinstance(headers, Mapping) else headers
    return {str(k).lower(): str(v).lower() for k, v in items}


def _visible_text(html: str) -> str:
    """Rough visible text: drop script/style, strip tags, collapse whitespace."""
    return " ".join(_ANYTAG.sub(" ", _TAGS.sub(" ", html)).split())


def _framework(html: str) -> str | None:
    for name, marker in _FRAMEWORKS:
        if marker in html:
            return name
    return None


def detect_anti_bot(
    status: int, headers: "Mapping[Any, Any]", cookie_names: "Iterable[str]", body_low: str
) -> str | None:
    """The anti-bot vendor *actually challenging* this response, or None -- a strong
    body interstitial on its own, or a vendor header/cookie on a blocking status
    (a marker on a normal 200 is just CDN traffic). A bare block status with no vendor
    is reported as ``"challenge"``. (Kept for callers that want the single verdict.)"""
    h = _lower_map(headers)
    header_blob = " ".join(h.keys()) + " " + " ".join(h.values())
    cookie_blob = " ".join(str(c).lower() for c in cookie_names)
    blocking = status in _CHALLENGE_STATUS
    for vendor, weak_hdrs, weak_cooks, strong_body in _ANTIBOT:
        if any(m in body_low for m in strong_body):
            return vendor
        if blocking and (
            any(s in header_blob for s in weak_hdrs)
            or any(c in cookie_blob for c in weak_cooks)
        ):
            return vendor
    if status in _CHALLENGE_STATUS:
        return "challenge"
    return None


def _vendor_fingerprint(h: dict[str, str], cookie_names: "Iterable[str]") -> str | None:
    """A vendor whose header/cookie is present on ANY status -- the vendor is in front
    of the site (``anti_bot_present``), whether or not it is challenging us now."""
    header_blob = " ".join(h.keys()) + " " + " ".join(h.values())
    cookie_blob = " ".join(str(c).lower() for c in cookie_names)
    for vendor, weak_hdrs, weak_cooks, _ in _ANTIBOT:
        if any(s in header_blob for s in weak_hdrs) or any(c in cookie_blob for c in weak_cooks):
            return vendor
    return None


def _vendor_interstitial(body_low: str) -> str | None:
    for vendor, _, _, strong_body in _ANTIBOT:
        if any(m in body_low for m in strong_body):
            return vendor
    return None


def _combine(confidences: "Iterable[float]") -> float:
    """Noisy-OR of confidences: corroborating evidence raises the total. 1 - Π(1-c)."""
    p = 1.0
    for c in confidences:
        p *= 1.0 - min(1.0, max(0.0, c))
    return round(1.0 - p, 3)


def build_flag(
    name: str, signals: "list[Signal]", *, remedy: str | None = None, value: Any = None
) -> "Flag":
    """Roll signals up into a flag: confidence = noisy-OR of the evidence; ``present``
    when it reaches the threshold; ``remedy`` only applies once present."""
    from ..core.document.models import Flag

    conf = _combine(s.confidence for s in signals)
    present = conf >= _PRESENT
    return Flag(
        name=name, present=present, confidence=conf, signals=list(signals),
        remedy=cast(Any, remedy) if present else None, value=value,
    )


def _decode(body: "bytes | str | None") -> str:
    if isinstance(body, (bytes, bytearray)):
        return body.decode("utf-8", "replace")
    return body or ""


def static_signals(
    status: int,
    headers: "Mapping[Any, Any] | Iterable[tuple[Any, Any]]",
    cookies: "Iterable[str] | Mapping[str, Any]",
    body: "bytes | str | None",
    redirect_chain: "Iterable[str]" = (),
) -> "list[Signal]":
    """Every request+static signal derivable without a render (front-loaded)."""
    from ..core.document.models import Signal

    text = _decode(body)
    low = text.lower()[:8000]
    hmap = _lower_map(headers)
    cookie_names = list(cookies.keys()) if isinstance(cookies, Mapping) else list(cookies)
    is_html = "html" in hmap.get("content-type", "") or "<html" in low or "<!doctype html" in low
    visible = _visible_text(text) if is_html else text
    has_script = "<script" in low
    chain = [str(u).lower() for u in redirect_chain]
    out: list[Signal] = []

    def sig(name: str, flag: str, stage: "Stage", conf: float, reason: str, value: Any = None) -> None:
        out.append(Signal(name=name, flag=flag, stage=stage, confidence=conf, reason=reason, value=value))

    # -- spa (static evidence; rendered/network added by the facet) --
    fw = _framework(text)
    if fw:
        sig("framework_marker", "spa", "static", 0.6, f"a {fw} client-render marker", fw)
    if _SPA_SHELL.search(text):
        sig("empty_root_shell", "spa", "static", 0.9, "an empty hydration root a bundle populates")
    if is_html and status == 200 and len(visible) < 40 and has_script:
        sig("empty_body_scripted", "spa", "static", 0.7, "a near-empty body with a script")
    if any(m in text for m in _HYDRATION_BLOBS):
        sig("hydration_state_blob", "spa", "static", 0.5, "a serialized initial-state blob")

    # -- anti_bot present (a vendor is in front, any status) --
    vendor = _vendor_fingerprint(hmap, cookie_names)
    if vendor:
        sig("vendor_fingerprint", "anti_bot_present", "request", 0.5,
            f"a {vendor} header/cookie", vendor)

    # -- anti_bot triggered (a challenge blocking us now) --
    interstitial = _vendor_interstitial(low)
    if interstitial:
        sig("challenge_interstitial", "anti_bot_triggered", "static", 0.95,
            f"a {interstitial} challenge page", interstitial)
    if status in _CHALLENGE_STATUS:
        sig("challenge_status", "anti_bot_triggered", "request", 0.6,
            f"a challenge status ({status})", status)
        if vendor:
            sig("vendor_on_block", "anti_bot_triggered", "request", 0.9,
                f"{vendor} on a blocking status", vendor)

    # -- login present / required --
    has_password = 'type="password"' in low or "type='password'" in low
    if has_password:
        sig("password_field", "login_present", "static", 0.6, "a password field")
    if _SIGNIN.search(low):
        sig("signin_link", "login_present", "static", 0.35, "a sign-in link/label")
    if status == 401:
        sig("status_401", "login_required", "request", 0.95, "a 401 unauthorized", 401)
    if has_password and is_html and len(visible) < 500:
        sig("password_on_sparse_page", "login_required", "static", 0.7,
            "a password field on a page with little other content")
    if len(chain) > 1 and any(p in chain[-1] for p in ("/login", "/signin", "/sign-in", "/auth")):
        sig("redirect_to_login", "login_required", "request", 0.8, "redirected to a login URL")

    return out


#: which flags detect fully or partly from request+static, and their remedy.
_STATIC_FLAGS = ("spa", "anti_bot_present", "anti_bot_triggered", "login_present", "login_required")


def static_flags(
    status: int,
    headers: "Mapping[Any, Any] | Iterable[tuple[Any, Any]]",
    cookies: "Iterable[str] | Mapping[str, Any]",
    body: "bytes | str | None",
    redirect_chain: "Iterable[str]" = (),
) -> "dict[str, Flag]":
    """The request/static flags, built from :func:`static_signals`. ``auto`` reads
    these on the first hop; the facet augments ``spa`` with rendered/network signals."""
    signals = static_signals(status, headers, cookies, body, redirect_chain)
    grouped: dict[str, list[Any]] = {}
    for s in signals:
        grouped.setdefault(s.flag, []).append(s)
    flags: dict[str, Any] = {}
    for name in _STATIC_FLAGS:
        group = grouped.get(name, [])
        remedy: str | None = None
        value: Any = None
        if name == "spa":
            remedy = "browser"
        elif name in ("anti_bot_present", "anti_bot_triggered"):
            # the vendor name (or "challenge") is the actionable payload.
            value = next((s.value for s in group if isinstance(s.value, str)), None)
            if name == "anti_bot_triggered":
                # a named vendor -> stealth (a bare challenge is a bad IP -> proxy).
                named = any(s.name in ("challenge_interstitial", "vendor_on_block") for s in group)
                remedy = "stealth" if named else "proxy"
        flags[name] = build_flag(name, group, remedy=remedy, value=value)
    return flags


__all__ = [
    "static_signals",
    "static_flags",
    "build_flag",
    "detect_anti_bot",
]
