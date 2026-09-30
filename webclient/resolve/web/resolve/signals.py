"""Signals -- a sub-part of resolve: clean, standalone detector functions.

Each signal is a plain pure function that returns a :class:`Signal` (or ``None``) from what it
needs -- mostly a parsed :class:`~web.parse.Document`. They are NOT lumped behind one uniform
interface: a caller (e.g. :func:`~web.resolve.escalate`) just calls the ones it cares about.
Signals live in resolve, not in parse (which stays strictly bytes -> Document); resolve uses them
to make policy decisions (escalate on ``spa``; a ``login_wall`` has no transport remedy).
"""

from __future__ import annotations

from pydantic import BaseModel, JsonValue
from web.fetch import Snapshot
from web.parse import Document


class Signal(BaseModel):
    """A named piece of evidence, with a confidence and free detail."""

    name: str
    confidence: float = 1.0
    detail: dict[str, JsonValue] = {}


def spa(doc: Document) -> "Signal | None":
    """A client-rendered shell: a mount node and scripts but little server-rendered text -- the
    content arrives via JS, so a static fetch sees an empty page (escalate to a browser).
    """
    if doc.kind != "html":
        return None
    body = doc.select("body")
    visible = len(body.text) if body is not None else 0
    mounts = doc.select("#root, #app, [data-reactroot], [data-server-rendered], [ng-version]")
    if visible < 200 and mounts is not None and doc.select("script") is not None:
        return Signal(name="spa", confidence=0.8, detail={"visible_chars": visible})
    return None


def login_wall(doc: Document) -> "Signal | None":
    """A login gate: a password field is present, so the content is behind auth (no transport
    remedy -- the caller must supply credentials/session)."""
    if doc.kind == "html" and doc.select("input[type=password]") is not None:
        return Signal(name="login_wall")
    return None


def pagination(doc: Document) -> "Signal | None":
    """The document is one page of many: a rel=next or a pagination control is present."""
    if (
        doc.kind == "html"
        and doc.select(
            "a[rel~=next], link[rel=next], .pagination a, nav.pager a, [aria-label=Next]"
        )
        is not None
    ):
        return Signal(name="pagination")
    return None


#: bot-wall COPY served in place of the content -- a real dataset page never says these. Cloudflare
#: "Just a moment" / "Checking your browser", generic JS-gate / DDoS walls, and the classic
#: "verify you are human" interstitials. A CONTENT wall is a browser-fingerprint verdict, so the
#: default remedy is to climb REALNESS (ANTI-BOT.md §2.3/§5), which is cheaper than swapping IP; a
#: block that persists as a bare 401/403 without this copy is the IP-reputation tier instead.
_CHALLENGE_TEXT = (
    "just a moment",
    "checking your browser",
    "verifying you are human",
    "verify you are human",
    "please wait while we verify",
    "enable javascript and cookies to continue",
    "ddos protection by",
    "attention required",
    "unusual traffic",
    "are you a robot",
    "cf-challenge",
)
#: challenge-platform script hosts / tokens (matched in raw HTML, not visible text): the Cloudflare
#: challenge platform + Turnstile, DataDome's captcha-delivery, and the `cf_chl` challenge markers.
_CHALLENGE_MARKUP = (
    "challenges.cloudflare.com",
    "challenge-platform",
    "captcha-delivery.com",
    "_cf_chl",
    "cf_chl_opt",
    "cf-mitigated",
)


def _visible_chars(doc: Document) -> int:
    body = doc.select("body")
    return len(body.text) if body is not None else 0


def js_challenge(doc: Document) -> "Signal | None":
    """An INVISIBLE / interactive JS or proof-of-work challenge INTERSTITIAL -- the middle of the
    provider escalation ladder (ANTI-BOT.md §4: passive score -> JS/PoW challenge -> interactive).
    A near-empty holder page served IN PLACE OF the content that runs JS to mint a clearance token:
    Cloudflare Managed Challenge / Turnstile, DataDome Device Check, Akamai/Kasada sensor shells.
    The remedy is a MORE REAL browser (climb the realness ladder), NOT a proxy -- this is a
    browser-fingerprint verdict (ANTI-BOT.md §2.3), not an IP one. Gated on low visible content so a
    normal page that merely embeds a Turnstile/reCAPTCHA form widget does not read as blocked."""
    if doc.kind != "html":
        return None
    if any(m in doc.text.lower() for m in _CHALLENGE_TEXT):
        return Signal(name="js_challenge", confidence=0.85)
    html = doc.content.decode("utf-8", "ignore").lower()
    if _visible_chars(doc) < 500 and any(h in html for h in _CHALLENGE_MARKUP):
        return Signal(name="js_challenge", confidence=0.8)
    return None


def captcha(doc: Document) -> "Signal | None":
    """A VISIBLE CAPTCHA puzzle -- the TOP of the provider ladder (ANTI-BOT.md §3 CAPTCHA layer):
    reCAPTCHA v2 image grid, hCaptcha, Arkose FunCaptcha / press-and-hold. Distinct from a silent
    :func:`js_challenge`: a HUMAN puzzle is being demanded, so no transport tier alone clears it --
    the remedy is a real browser and (ultimately) a solver, or avoiding the flow. Detected by the
    puzzle's own prompt text, or a vendor widget on an otherwise near-empty interstitial (a full page
    that merely embeds a form captcha is NOT blocked)."""
    if doc.kind != "html":
        return None
    text = doc.text.lower()
    if any(
        m in text
        for m in (
            "select all images",
            "select each image",
            "i'm not a robot",
            "press & hold",
            "press and hold",
        )
    ):
        return Signal(name="captcha", confidence=0.8)
    if _visible_chars(doc) < 500 and (
        doc.select(
            ".g-recaptcha, .h-captcha, iframe[src*=recaptcha], iframe[src*=hcaptcha], "
            "script[src*=arkoselabs], script[src*=funcaptcha]"
        )
        is not None
    ):
        return Signal(name="captcha", confidence=0.75)
    return None


def consent_wall(doc: Document) -> "Signal | None":
    """A cookie / GDPR consent gate: a consent banner is present and may overlay the content until
    dismissed (the remedy is an interaction, not a transport change)."""
    if doc.kind != "html":
        return None
    hit = doc.select(
        "#onetrust-banner-sdk, #cookie-consent, [id*=cookie-banner], "
        "[class*=cookie-consent], [aria-label*=consent], [data-consent]"
    )
    if hit is not None:
        return Signal(name="consent_wall", confidence=0.6)
    return None


def infinite_scroll(doc: Document) -> "Signal | None":
    """The list grows on scroll rather than via a pager: a scroll sentinel / infinite-scroll hook
    is present (the remedy is a scroll loop on a live page, not a URL walk)."""
    if doc.kind != "html":
        return None
    if (
        doc.select(
            "[data-infinite-scroll], .infinite-scroll, [data-infinite], "
            "[class*=infinite-scroll], .load-more[data-scroll]"
        )
        is not None
    ):
        return Signal(name="infinite_scroll", confidence=0.6)
    return None


def empty(doc: Document) -> "Signal | None":
    """A near-empty document: almost no readable text (a failed render, a blank shell, or a body
    that never populated). Distinct from ``spa`` -- this is 'nothing here', regardless of scripts.
    """
    if doc.kind != "html":
        return None
    body = doc.select("body")
    chars = len(body.text) if body is not None else 0
    if chars < 50:
        return Signal(name="empty", confidence=0.7, detail={"visible_chars": chars})
    return None


def denied_status(snap: Snapshot) -> "Signal | None":
    """A hard transport DENY -- 401/403. Per ANTI-BOT.md §2.2 an IP/ASN reputation block is checked
    BEFORE the fingerprint and surfaces as a 401/403 with no challenge served, so the remedy is a
    residential/mobile IP, not more browser realness. (A 403 that CARRIES a challenge is a realness
    verdict instead -- the flag layer's contra evidence separates the two.)"""
    if snap.status in (401, 403):
        return Signal(name="denied_status", confidence=0.9, detail={"status": snap.status})
    return None


def rate_limited(snap: Snapshot) -> "Signal | None":
    """A 429 rate-limit / tarpit tier (ANTI-BOT.md §4). The remedy is to BACK OFF and retry the SAME
    identity -- climbing the realness ladder or swapping IP wastes budget on a throughput limit, which
    a realer transport does not fix."""
    if snap.status == 429:
        return Signal(name="rate_limited", confidence=0.85, detail={"status": 429})
    return None


def server_error(snap: Snapshot) -> "Signal | None":
    """A 5xx or a transport failure -- transient, worth a retry rather than a tier climb."""
    if snap.error is not None or 500 <= snap.status < 600:
        return Signal(
            name="server_error",
            confidence=0.9,
            detail={
                "status": snap.status,
                "error": snap.error.code if snap.error else None,
            },
        )
    return None


def structured_data(doc: Document) -> "Signal | None":
    """The page publishes machine-readable data about itself -- JSON-LD (``ld+json``) or microdata
    (``itemscope``). Prefer extracting THAT over scraping the rendered DOM."""
    if doc.kind != "html":
        return None
    if doc.metadata().ld_json or doc.select("[itemscope]") is not None:
        return Signal(name="structured_data", confidence=0.9)
    return None


def data_api(doc: Document) -> "Signal | None":
    """The page carries its data as an inlined JSON blob (a ``__NEXT_DATA__`` / state island /
    ``application/json`` script) rather than only as DOM -- read the island, don't render.
    """
    if doc.kind != "html":
        return None
    if (
        doc.select("script#__NEXT_DATA__, script[type='application/json'], script[id*=state]")
        is not None
    ):
        return Signal(name="data_api", confidence=0.7)
    return None


def record_list(doc: Document) -> "Signal | None":
    """A repeating record region -- the DATASET -- is present (via :meth:`Document.records`). The
    detail carries the suggested ``select_all`` item selector and the record count."""
    if doc.kind != "html":
        return None
    regions = doc.records(top_k=1)
    if not regions:
        return None
    top = regions[0]
    return Signal(
        name="record_list",
        confidence=min(0.95, 0.5 + top.count * 0.02),
        detail={"item_selector": top.item_selector, "count": top.count},
    )


def tabbed(doc: Document) -> "Signal | None":
    """A tab widget: some records may live behind a tab that only populates the DOM on click (the
    remedy is an interaction, not a URL walk)."""
    if doc.kind != "html":
        return None
    if doc.select("[role=tablist], [role=tab], [data-tabs], [data-tab], .tab-content") is not None:
        return Signal(name="tabbed", confidence=0.6)
    return None


def iframe(doc: Document) -> "Signal | None":
    """The content sits inside an ``<iframe>`` -- the fetched document is a frame around it, so a
    read must descend into the framed source."""
    if doc.kind != "html":
        return None
    frames = doc.select_all("iframe[src]")
    if frames:
        return Signal(name="iframe", confidence=0.5, detail={"count": len(frames)})
    return None


__all__ = [
    "Signal",
    "spa",
    "login_wall",
    "pagination",
    "js_challenge",
    "captcha",
    "consent_wall",
    "infinite_scroll",
    "empty",
    "denied_status",
    "rate_limited",
    "server_error",
    "structured_data",
    "data_api",
    "record_list",
    "tabbed",
    "iframe",
]
