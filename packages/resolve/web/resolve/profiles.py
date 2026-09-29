"""Named default resolve POLICIES -- sane, reusable :class:`Profile`\\s.

A resolve profile is a POLICY bundle: a transport ladder (built from :mod:`web.fetch.profiles`
identities) + escalation + fingerprint rotation + retry + politeness. These are the sensible
defaults a caller reaches for; ``.with_(...)`` to adjust. The proxy variants need a real endpoint,
so they are factories, not constants.

  BASIC              HTTP, rotating identity per request (cheap; the default)
  BASIC_BROWSER      HTTP first, escalate to a browser on a block signal (rotating HTTP identity)
  FULL_BROWSER       always render in a browser (stable per-session identity)
  proxy(server)      / proxy_browser(server) / proxy_full_browser(server)  -- the same, via a proxy
"""

from __future__ import annotations

from web.fetch import Proxy
from web.fetch import profiles as _fp

from .base import Profile

#: HTTP with a fresh (browserforge) identity per request -- the cheap default.
BASIC = Profile(ladder=(_fp.BASIC,), rotate=True, retry=3, rate_limit=0.5)
#: HTTP first, escalate to a browser render when a page looks blocked / JS-gated; rotating HTTP id.
BASIC_BROWSER = Profile(ladder=(_fp.BASIC, _fp.BROWSER), rotate=True, retry=2, rate_limit=0.5)
#: always render in a real browser (a stable per-session identity, no rotation).
FULL_BROWSER = Profile(ladder=(_fp.BROWSER,), retry=2, rate_limit=0.5)


def proxy(server: "str | Proxy") -> Profile:
    """:data:`BASIC` routed through ``server``."""
    return BASIC.with_(ladder=(_fp.proxy(server),))


def proxy_browser(server: "str | Proxy") -> Profile:
    """:data:`BASIC_BROWSER` routed through ``server`` (both tiers)."""
    return BASIC_BROWSER.with_(ladder=(_fp.proxy(server), _fp.proxy_browser(server)))


def proxy_full_browser(server: "str | Proxy") -> Profile:
    """:data:`FULL_BROWSER` routed through ``server``."""
    return FULL_BROWSER.with_(ladder=(_fp.proxy_browser(server),))


__all__ = ["BASIC", "BASIC_BROWSER", "FULL_BROWSER", "proxy", "proxy_browser", "proxy_full_browser"]
