"""WebClient's model + interface, and the value models the client backings
produce.

``IWebClient`` is the client's Core Fields (policy) plus, under ``TYPE_CHECKING``,
the eager authoring verbs it implements (``fetch`` / ``ref`` / ``search`` /
``summary``, generated from the client backings). ``WebClient`` inherits it
and adds the machinery (loop, pool, bus, transport, plan execution). The verbs are
``TYPE_CHECKING``-only, so at runtime this is just the policy model and
``WebCore.__getattr__`` dispatches every verb.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict

from ..reference.models import Resolve

if TYPE_CHECKING:
    from ...collection import Collection  # noqa: F401  (sitemaps -> Collection)
    from ..document import Document  # noqa: F401  (fetch -> Document)
    from ..document.models import Summary  # noqa: F401  (summary -> Summary)
    from ..reference import Reference  # noqa: F401  (ref/sitemaps -> Reference)


class SearchResult(BaseModel):
    """One search hit: ``title`` / ``url`` / ``description`` as the provider gave
    them, plus ``rank`` (1-based position on the results page)."""

    rank: int = 0
    title: str = ""
    url: str = ""
    description: str = ""


class IWebClient(BaseModel):
    """The client's Core Fields (policy), plus (for the checker) the eager
    authoring verbs ``WebClient`` implements -- ``fetch`` / ``ref`` / ``search``
    / ``summary``. The verbs are ``TYPE_CHECKING``-only, so at runtime this is just
    the policy model."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    timeout: float = 30.0
    default_headers: dict[str, str] = {}
    names_cap: int | None = None
    block_private_hosts: bool = False  # opt-in SSRF guard (loopback/private/etc.)
    retries: int = 0  # extra attempts on a retriable failure (transport/429/5xx)
    retry_backoff: float = 0.2  # base seconds; doubled each attempt (exp backoff)
    min_interval: float = 0.0  # per-host politeness: min seconds between requests
    # the resiliency policy bundle: when set, its rate/retry/proxy concerns are
    # declared to a downstream proxy service as X-WebClient-* request headers
    # (the service is assumed to exist and enforce the network-level parts).
    resolve: Resolve | None = None

    if TYPE_CHECKING:
        # >>> generated: WebClient interface <<<
        # fmt: off
        def fetch(self, url: Any, *, browser: "bool | Literal['never', 'auto', 'always', 'probe']" = ..., optional: bool = ..., error: Any = ..., **kw: Any) -> "Document": ...
        def ref(self, url: Any, method: str = ..., **kw: Any) -> "Reference": ...
        def search(self, query: str, *, limit: int = ..., endpoint: str | None = ..., optional: bool = ..., error: Any = ...) -> "list[SearchResult]": ...
        def sitemaps(self, url: Any, *, limit: int = ...) -> "Collection[Reference]": ...
        def summary(self, url: Any, *include: str, **kw: Any) -> "Summary": ...
        # fmt: on
        # >>> end generated <<<
        pass


__all__ = ["SearchResult", "IWebClient"]
