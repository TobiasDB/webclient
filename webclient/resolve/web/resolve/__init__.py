"""web.resolve -- orchestration: ``Request -> Document``, and the middleware implementations.

The middleware FRAMEWORK lives in web.fetch (the ``Middleware`` type + ``stack``); this layer
owns the IMPLEMENTATIONS and composes them into per-vendor profiles. A :class:`Resolver` stacks a
middleware chain around a base Fetcher and parses the resulting Snapshot into a Document.

    from web.fetch import Request, profiles as fp
    from web.resolve import Resolver, Profile, EscalationPolicy, RetryPolicy, RatePolicy
    acme = Profile(
        escalation=EscalationPolicy(tiers=(fp.BASIC, fp.BROWSER)),
        retry=RetryPolicy(max_attempts=2),
        rate=RatePolicy(per_host=0.5),
    )
    doc = await Resolver(profile=acme).resolve(Request(url="https://acme.com"))

The provided middlewares (retry / rate_limit / escalate / paginate_*) are REFERENCE
implementations -- a consumer writes their own ``async (request, next) -> Snapshot`` and stacks it
the same way; the framework does not care. Signals are purely-functional Document detectors used
by escalate (parse stays strictly bytes -> Document); stops compose pagination end conditions.
"""

from __future__ import annotations

# the framework, re-exported for ergonomics (it lives in web.fetch)
from web.fetch import Handler, Middleware, stack

from . import (  # named default resolve policies (web.resolve.profiles.BASIC / FULL_BROWSER / ...)
    profiles,
)
from .base import Profile, Resolver, Slot, Tier
from .document import document
from .entry import ResolveSession, resolve
from .flags import Flag, flags
from .middleware import escalate, rate_limit, retry, rotate, transport_remedy
from .models import ResolveEvent
from .paginate import (
    Until,
    paginate_clicks,
    paginate_cursor,
    paginate_links,
    paginate_param,
)
from .policy import (
    EscalationPolicy,
    PaginatePolicy,
    Policy,
    RatePolicy,
    RetryPolicy,
    RotationPolicy,
)
from .signals import (
    Signal,
    captcha,
    consent_wall,
    data_api,
    denied_status,
    empty,
    iframe,
    infinite_scroll,
    js_challenge,
    login_wall,
    pagination,
    rate_limited,
    record_list,
    server_error,
    spa,
    structured_data,
    tabbed,
)
from .stops import any_of, first_n, until_empty, until_match, until_repeat

__all__ = [
    "Resolver",
    "resolve",
    "ResolveSession",
    "Profile",
    "profiles",
    "Slot",
    "Tier",
    "Policy",
    "RetryPolicy",
    "RatePolicy",
    "RotationPolicy",
    "PaginatePolicy",
    "EscalationPolicy",
    "Middleware",
    "Handler",
    "stack",
    # middleware implementations (consumer-pluggable; these are the reference ones)
    "retry",
    "rate_limit",
    "escalate",
    "rotate",
    "transport_remedy",
    "paginate_links",
    "paginate_param",
    "paginate_clicks",
    "paginate_cursor",
    "Until",
    # pagination stop conditions (composable, some stateful)
    "until_empty",
    "until_match",
    "first_n",
    "until_repeat",
    "any_of",
    # signals (evidence) + flags (conclusions with remedies)
    "document",
    "ResolveEvent",
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
    "Flag",
    "flags",
]
