"""web.resolve -- orchestration: ``Request -> Document`` with policies + middleware.

Ties the lower layers together: fetch a Snapshot, parse it to a Document, wrapped in a
middleware chain of policies (retry, rate limiting, later browser escalation). The base step
is just ``fetch -> parse``; middleware is the onion around it and the extension point.

    from web.fetch import HttpFetcher, Request
    from web.resolve import Resolver, retry, rate_limit
    r = Resolver(HttpFetcher(), middleware=(rate_limit(0.5), retry(3)))
    doc = await r.resolve(Request(url="https://example.com"))

Depends only on the lower layers (kernel, fetch, parse) -- it knows their interfaces, not
httpx or lxml. A sync / lazy / remote face is the DSL's job, not this layer's.
"""

from __future__ import annotations

from .base import Handler, Middleware, Resolver
from .middleware import escalate, rate_limit, retry
from .paginate import paginate_clicks, paginate_links, paginate_param
from .signals import DETECTORS, Signal, detect, login_wall, pagination, spa
from .stops import any_of, first_n, until_empty, until_match, until_repeat

__all__ = [
    "Resolver", "Handler", "Middleware", "retry", "rate_limit", "escalate",
    # pagination: middleware strategies + composable stop conditions
    "paginate_links", "paginate_param", "paginate_clicks",
    "until_empty", "until_match", "first_n", "until_repeat", "any_of",
    # signals: purely-functional detectors over a Document (parse stays bytes -> Document)
    "Signal", "detect", "spa", "login_wall", "pagination", "DETECTORS",
]
