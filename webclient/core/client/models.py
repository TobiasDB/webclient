"""WebClientCore's model + interface, and the value models the client backings
produce.

``IWebClient`` is the client's Core Fields (policy) plus, under ``TYPE_CHECKING``,
the eager authoring verbs it implements (``fetch`` / ``ref`` / ``search`` /
``summary``, generated from the client backings). ``WebClientCore`` inherits it
and adds the machinery (loop, pool, bus, transport, plan execution). The verbs are
``TYPE_CHECKING``-only, so at runtime this is just the policy model and
``WebCore.__getattr__`` dispatches every verb.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    from ..document import DocumentCore  # noqa: F401  (fetch -> Document)
    from ..document.models import Summary  # noqa: F401  (summary -> Summary)
    from ..reference import ReferenceCore  # noqa: F401  (ref -> Reference)


class SearchResult(BaseModel):
    """One search hit: ``title`` / ``url`` / ``description`` as the provider gave
    them, plus ``rank`` (1-based position on the results page)."""

    rank: int = 0
    title: str = ""
    url: str = ""
    description: str = ""


class IWebClient(BaseModel):
    """The client's Core Fields (policy), plus (for the checker) the eager
    authoring verbs ``WebClientCore`` implements -- ``fetch`` / ``ref`` / ``search``
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

    if TYPE_CHECKING:
        # >>> generated: WebClient interface <<<
        # fmt: off
        def fetch(self, url: Any, *, optional: bool = ..., error: Any = ..., **kw: Any) -> "DocumentCore": ...
        def ref(self, url: Any, method: str = ..., **kw: Any) -> "ReferenceCore": ...
        def search(self, query: str, *, limit: int = ..., endpoint: str | None = ...) -> "list[SearchResult]": ...
        def summary(self, url: Any, *include: str, **kw: Any) -> "Summary": ...
        # fmt: on
        # >>> end generated <<<
        pass


__all__ = ["SearchResult", "IWebClient"]
