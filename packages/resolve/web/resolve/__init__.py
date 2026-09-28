"""web.resolve -- orchestration: ``Request -> Document``, and the middleware implementations.

The middleware FRAMEWORK lives in web.fetch (the ``Middleware`` type + ``stack``); this layer
owns the IMPLEMENTATIONS and composes them into per-vendor profiles. A :class:`Resolver` stacks a
middleware chain around a base Fetcher and parses the resulting Snapshot into a Document.

    from web.fetch import HttpFetcher, BrowserFetcher, Request
    from web.resolve import Resolver, profile, rate_limit, retry, escalate, paginate_links
    acme = Profile(rate_limit=0.5, retry=3, escalate=BrowserFetcher(), paginate=paginate_links())
    doc = await Resolver(HttpFetcher(), profile=acme).resolve(Request(url="https://acme.com"))

The provided middlewares (retry / rate_limit / escalate / paginate_*) are REFERENCE
implementations -- a consumer writes their own ``async (request, next) -> Snapshot`` and stacks it
the same way; the framework does not care. Signals are purely-functional Document detectors used
by escalate (parse stays strictly bytes -> Document); stops compose pagination end conditions.
"""

from __future__ import annotations

# the framework, re-exported for ergonomics (it lives in web.fetch)
from web.fetch import Handler, Middleware, stack

from .base import Profile, Resolver
from .middleware import escalate, rate_limit, retry
from .paginate import Until, paginate_clicks, paginate_links, paginate_param
from .signals import DETECTORS, Signal, detect, login_wall, pagination, spa
from .stops import any_of, first_n, until_empty, until_match, until_repeat

__all__ = [
    "Resolver", "Profile", "Middleware", "Handler", "stack",
    # middleware implementations (consumer-pluggable; these are the reference ones)
    "retry", "rate_limit", "escalate",
    "paginate_links", "paginate_param", "paginate_clicks", "Until",
    # pagination stop conditions (composable, some stateful)
    "until_empty", "until_match", "first_n", "until_repeat", "any_of",
    # signals: purely-functional detectors over a Document
    "Signal", "detect", "spa", "login_wall", "pagination", "DETECTORS",
]
