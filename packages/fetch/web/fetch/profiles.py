"""Named default transport identities -- sane, reusable :class:`Profile`\\s.

A fetch profile is a TRANSPORT IDENTITY (proxy / fingerprint / browser) -- the building block a
resolve policy profile composes into a ladder. These are the sensible defaults; ``.with_(...)`` to
adjust, and the ``proxy_*`` factories add a proxy (which needs a real endpoint, so it is an
argument, not a constant). Fingerprints come from browserforge (``fingerprint=True``); ROTATION
across requests is a resolve middleware, not an identity, so it is not here."""

from __future__ import annotations

from .entry import Profile
from .proxy import Proxy


#: HTTP transport with a realistic (browserforge) identity -- the cheap default.
BASIC = Profile(fingerprint=True)
#: a real browser render (Playwright) with a realistic identity -- for JS-gated pages.
BROWSER = Profile(fingerprint=True, browser=True)


def with_proxy(base: Profile, proxy: "str | Proxy") -> Profile:
    """``base`` routed through ``proxy`` (a server string or a :class:`Proxy`)."""
    return base.with_(proxy=proxy)


def proxy(server: "str | Proxy") -> Profile:
    """HTTP through a proxy, with a realistic identity."""
    return BASIC.with_(proxy=server)


def proxy_browser(server: "str | Proxy") -> Profile:
    """A browser render through a proxy."""
    return BROWSER.with_(proxy=server)


__all__ = ["BASIC", "BROWSER", "with_proxy", "proxy", "proxy_browser"]
