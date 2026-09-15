"""Pure response classification for the resiliency ladder.

``classify(status, headers, cookies, body)`` reads a raw response and reports the
signals a resolver cares about -- which anti-bot vendor (if any) is in play,
whether the page is JS-gated / empty, whether it is a hard block, a paywall or a
login wall. **Pure** (no cores, no IO, no lxml) so a local and a remote resolve
agree, and conservative (high-confidence markers only) so it does not pollute the
``probe`` facet with false positives on ordinary pages.

Body markers are read first, never the ``Server`` header alone. Anti-bot
fingerprints follow the public detection markers (Cloudflare / DataDome /
PerimeterX / Akamai / Kasada / AWS WAF / Incapsula).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

_TAGS = re.compile(r"<(script|style)\b[^>]*>.*?</\1>", re.I | re.S)
_ANYTAG = re.compile(r"<[^>]+>")
_SPA_SHELL = re.compile(
    r'<(?:div|main|app-root|#document)[^>]*\bid=["\'](?:root|app|__next|__nuxt)["\']'
    r'[^>]*>\s*</',
    re.I,
)


@dataclass(frozen=True)
class Signals:
    """What a raw response tells a resolver. ``anti_bot`` is a vendor name or None."""

    anti_bot: str | None = None
    js_required: bool = False  # page needs a browser (empty / SPA shell)
    empty: bool = False  # suspiciously little visible text on a 200 html page
    blocked: bool = False  # a hard block: 401 / 403 / 429
    paywall: bool = False
    login_wall: bool = False

    @property
    def any(self) -> bool:
        """Whether anything worth recording was detected (else leave probe None)."""
        return bool(
            self.anti_bot
            or self.js_required
            or self.empty
            or self.blocked
            or self.paywall
            or self.login_wall
        )

    @property
    def needs_browser(self) -> bool:
        """A browser tier would plausibly help (the page is JS-gated; not a hard
        block or a wall, which a browser alone will not fix)."""
        return self.js_required


#: (vendor, header name/value substrings, cookie-name substrings, body substrings)
_ANTIBOT: tuple[tuple[str, tuple[str, ...], tuple[str, ...], tuple[str, ...]], ...] = (
    (
        "cloudflare",
        ("cf-ray", "cf-mitigated"),
        ("__cf_bm", "cf_clearance"),
        ("just a moment", "checking your browser", "cf-browser-verification"),
    ),
    ("datadome", ("x-datadome", "x-dd-"), ("datadome",), ("datadome",)),
    ("perimeterx", (), ("_px", "_pxhd", "_pxappid"), ("px-captcha", "perimeterx")),
    ("akamai", ("akamai-grn",), ("_abck", "ak_bmsc"), ("akamaighost",)),
    ("kasada", ("x-kpsdk",), (), ("kasada",)),
    ("awswaf", ("x-amzn-waf",), ("aws-waf-token",), ()),
    ("incapsula", ("x-iinfo",), ("visid_incap", "incap_ses"), ("incapsula incident",)),
)


def _lower_map(headers: "Mapping[Any, Any] | Iterable[tuple[Any, Any]]") -> dict[str, str]:
    items = headers.items() if isinstance(headers, Mapping) else headers
    return {str(k).lower(): str(v).lower() for k, v in items}


def _visible_text(html: str) -> str:
    """Rough visible text: drop script/style, strip tags, collapse whitespace."""
    return " ".join(_ANYTAG.sub(" ", _TAGS.sub(" ", html)).split())


#: statuses that are, on their own, evidence of a bot block / challenge (a bare
#: 403/429/503 with no vendor fingerprint -- e.g. an origin WAF or a rate-limit
#: gate). 401 stays out: that is an auth/login wall, not an anti-bot challenge.
_CHALLENGE_STATUS = frozenset({403, 429, 503})


def detect_anti_bot(
    status: int,
    headers: "Mapping[Any, Any]",
    cookie_names: "Iterable[str]",
    body_low: str,
) -> str | None:
    """The anti-bot vendor *actually challenging* this response, or None. A strong
    body marker (an interstitial page) counts on its own; the weak markers (a
    header/cookie that vendor sets on *all* its traffic -- e.g. Cloudflare's
    ``cf-ray`` sits on every proxied 200) count only on a blocking status. So a page
    merely served through a CDN is not mistaken for a block. Failing a named vendor,
    a bare block *status* (403/429/503) is itself reported as a generic
    ``"challenge"`` -- the caller still gets an anti-bot signal to escalate on."""
    h = _lower_map(headers)
    header_blob = " ".join(h.keys()) + " " + " ".join(h.values())
    cookie_blob = " ".join(str(c).lower() for c in cookie_names)
    # a 401 is a login/auth wall, NOT an anti-bot challenge, so a vendor cookie on a
    # 401 must not double-label it as anti-bot -- only 403/429/503 count as a block.
    blocking = status in _CHALLENGE_STATUS
    for vendor, weak_hdrs, weak_cooks, strong_body in _ANTIBOT:
        if any(m in body_low for m in strong_body):
            return vendor  # a challenge / interstitial page -> definite
        if blocking and (
            any(s in header_blob for s in weak_hdrs)
            or any(c in cookie_blob for c in weak_cooks)
        ):
            return vendor  # the vendor is present on a blocking response
    if status in _CHALLENGE_STATUS:
        return "challenge"  # a bare block status: no vendor named, still a block
    return None


def classify(
    status: int,
    headers: "Mapping[Any, Any] | Iterable[tuple[Any, Any]]",
    cookies: "Iterable[str] | Mapping[str, Any]",
    body: "bytes | str | None",
) -> Signals:
    """Classify a raw response into :class:`Signals` (conservative: only
    high-confidence markers). ``cookies`` may be cookie names or a name->value map."""
    text = (
        body.decode("utf-8", "replace")
        if isinstance(body, (bytes, bytearray))
        else (body or "")
    )
    low = text.lower()[:8000]
    cookie_names = list(cookies.keys()) if isinstance(cookies, Mapping) else list(cookies)
    hmap = _lower_map(headers)
    is_html = "html" in hmap.get("content-type", "")

    anti_bot = detect_anti_bot(status, hmap, cookie_names, low)
    blocked = status in (401, 403, 429)

    # empty / JS-gated: an SPA shell (empty root + a bundle) or a 200 html page with
    # almost no visible text but a script that would populate it. A small-but-real
    # page (has text) is NOT flagged -- a browser tier would not help it.
    visible = _visible_text(text) if is_html else text
    has_script = "<script" in low
    empty = is_html and status == 200 and len(visible) < 40
    js_required = is_html and (
        bool(_SPA_SHELL.search(text)) or (empty and has_script)
    )

    paywall = status == 402 or (
        "isaccessibleforfree" in low and re.search(r'isaccessibleforfree"?\s*:\s*false', low) is not None
    )
    # a login WALL, not merely a page that happens to carry a login form in its
    # header: a 401, or a password field on a login-focused page (little other
    # visible content). A content page with a header sign-in form is not flagged.
    has_password = 'type="password"' in low or "type='password'" in low
    login_wall = status == 401 or (has_password and len(visible) < 500)

    return Signals(
        anti_bot=anti_bot,
        js_required=js_required,
        empty=empty,
        blocked=blocked,
        paywall=paywall,
        login_wall=login_wall,
    )


__all__ = ["Signals", "classify", "detect_anti_bot"]
