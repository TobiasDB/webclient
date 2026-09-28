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
from .middleware import rate_limit, retry

__all__ = ["Resolver", "Handler", "Middleware", "retry", "rate_limit"]
