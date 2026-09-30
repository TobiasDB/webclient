"""Named default transport identities -- sane, reusable :class:`Profile`\\s.

A fetch profile is a TRANSPORT IDENTITY (proxy / fingerprint / browser) -- the building block a
resolve policy profile composes into a ladder. These are the sensible defaults; ``.with_(...)`` to
adjust, and the ``proxy_*`` factories add a proxy (which needs a real endpoint, so it is an
argument, not a constant). Fingerprints come from browserforge (``fingerprint=True``); ROTATION
across requests is a resolve middleware, not an identity, so it is not here."""

from __future__ import annotations

from .base import Profile
from .proxy import Proxy

# The BROWSER-REALNESS ladder: each rung is a more authentic (and heavier) browser identity, so a
# resolve escalation can climb from the cheapest render to the most convincing when a page keeps
# blocking. All carry a full browserforge fingerprint (navigator / WebGL / canvas), so even the
# bottom rung looks like a real Chrome to a fingerprinting probe.
#
#   BROWSER         headless bundled Chromium   -- cheap, fast; the usual render
#   HEADED_BROWSER  HEADED bundled Chromium     -- a real window; far fewer headless tells
#   REAL_CHROME     HEADED real Chrome channel  -- the genuine installed Chrome; most authentic

#: the cheap HTTP tier: a real Chrome TLS/HTTP2 fingerprint (curl_cffi) when the extra is installed,
#: else plain httpx with a browserforge header identity. curl_cffi closes the JA3/JA4 + HTTP2 tell
#: (ANTI-BOT.md §2.1) at NO more cost than an HTTP fetch and dominates stock httpx, so there is no
#: reason for a separate httpx-first rung -- httpx is the availability FALLBACK, not a tier you climb
#: through (see :func:`Profile.fetcher`). Still runs no JS, so a JS/PoW challenge needs the browser
#: rungs. ``impersonate`` + ``fingerprint`` are BOTH set so whichever backend is chosen has coherent
#: headers (curl_cffi carries its own matched Chrome headers; httpx uses the browserforge set).
BASIC = Profile(fingerprint=True, impersonate="chrome")
# Every browser rung runs through the leak-patched driver (``stealth=True`` -> patchright), which
# suppresses the CDP ``Runtime.enable`` leak (ANTI-BOT.md §5). It reuses the installed Chromium and
# falls back to stock Playwright when patchright isn't installed, so this is a safe default.
#: a real browser render (headless bundled Chromium) with a realistic identity -- for JS-gated pages.
BROWSER = Profile(fingerprint=True, browser=True, stealth=True)
#: a HEADED bundled-Chromium render -- a real on-screen window, which sheds the headless tells an
#: anti-bot WAF checks; the rung above :data:`BROWSER` (needs a display / Xvfb on Linux).
HEADED_BROWSER = Profile(fingerprint=True, browser=True, headless=False, stealth=True)
#: the genuine, installed Chrome (the ``chrome`` channel), HEADED -- the most authentic identity and
#: the top rung; needs Chrome installed on the host.
REAL_CHROME = Profile(
    fingerprint=True, browser=True, headless=False, channel="chrome", stealth=True
)


def with_proxy(base: Profile, proxy: "str | Proxy") -> Profile:
    """``base`` routed through ``proxy`` (a server string or a :class:`Proxy`)."""
    return base.with_(proxy=proxy)


def proxy(server: "str | Proxy") -> Profile:
    """HTTP through a proxy, with a realistic identity."""
    return BASIC.with_(proxy=server)


def proxy_browser(server: "str | Proxy") -> Profile:
    """A browser render through a proxy."""
    return BROWSER.with_(proxy=server)


def real_chrome(server: "str | Proxy") -> Profile:
    """The genuine installed Chrome routed through a proxy -- the most authentic tier, via a proxy."""
    return REAL_CHROME.with_(proxy=server)


__all__ = [
    "BASIC",
    "BROWSER",
    "HEADED_BROWSER",
    "REAL_CHROME",
    "with_proxy",
    "proxy",
    "proxy_browser",
    "real_chrome",
]
