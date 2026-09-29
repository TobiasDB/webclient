"""Named default resolve POLICIES -- sane, reusable :class:`Profile`\\s.

A resolve profile is a POLICY bundle: a transport ladder (built from :mod:`web.fetch.profiles`
identities) + escalation + fingerprint rotation + retry + politeness. These are the sensible
defaults a caller reaches for; ``.with_(...)`` to adjust. The proxy variants need a real endpoint,
so they are factories, not constants.

  BASIC              HTTP with a realistic, STABLE identity (cheap; the default)
  BASIC_BROWSER      HTTP first, escalate to a browser on a block signal
  FULL_BROWSER       always render in a browser
  proxy(server)      / proxy_browser(server) / proxy_full_browser(server)  -- the same, via a proxy

Fingerprint ROTATION is OFF by default: rotating identities from a SINGLE IP for the same URL is a
block signal, so it is only sane paired with IP rotation (a rotating proxy). Opt in explicitly
(``.with_(rotate=True)`` / ``resolve(rotate=True)``) when your proxy rotates IPs.
"""

from __future__ import annotations

from web.fetch import Proxy
from web.fetch import profiles as _fp

from .base import Profile

#: HTTP with a realistic (browserforge), STABLE identity -- the cheap default.
BASIC = Profile(ladder=(_fp.BASIC,), retry=3, rate_limit=0.5)
#: HTTP first, escalate to a browser render when a page looks blocked / JS-gated.
BASIC_BROWSER = Profile(ladder=(_fp.BASIC, _fp.BROWSER), retry=2, rate_limit=0.5)
#: always render in a real browser (a stable per-session identity).
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


#: the by-NAME registry (the constant profiles) -- so a serialisable lazy plan can name a policy,
#: e.g. ``wq.reference(url).resolve(profile="full_browser")``. Proxy variants need an endpoint, so
#: they are not name-addressable.
_REGISTRY: "dict[str, Profile]" = {"basic": BASIC, "basic_browser": BASIC_BROWSER, "full_browser": FULL_BROWSER}


def get(name: str) -> "Profile | None":
    """The named default policy (case-/dash-insensitive), or ``None`` if unknown."""
    return _REGISTRY.get(name.lower().replace("-", "_"))


__all__ = ["BASIC", "BASIC_BROWSER", "FULL_BROWSER", "proxy", "proxy_browser", "proxy_full_browser", "get"]
