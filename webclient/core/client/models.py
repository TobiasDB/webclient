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

from ..reference.models import BrowserConfig, Resolve

if TYPE_CHECKING:
    from ...clients import WaitConfig  # noqa: F401  (fetch wait strategy)
    from ...collection import Collection  # noqa: F401  (sitemap -> Collection)
    from ..document import Document  # noqa: F401  (fetch -> Document)
    from ..reference import Reference  # noqa: F401  (ref/sitemap -> Reference)


class Robots(BaseModel):
    """A site's ``robots.txt``, hunted from its origin (``wc.robots(url)``). When the
    site serves none, ``exists`` is False -- nothing is disallowed, so ``allowed`` is
    always True. ``sitemaps`` are the declared ``Sitemap:`` URLs; the raw ``content``
    is kept so the rules parse anywhere (including after crossing the wire)."""

    url: str = ""  # the robots.txt URL that was fetched
    exists: bool = False  # whether the site actually served a robots.txt
    content: str = ""  # the raw body (retained so allowed()/delay() work over the wire)
    sitemaps: list[str] = []  # the Sitemap: directive URLs (seed a crawl from these)

    def _parser(self) -> Any:
        from urllib.robotparser import RobotFileParser

        rp = RobotFileParser()
        rp.parse(self.content.splitlines())
        return rp

    def allowed(self, url: str, agent: str = "*") -> bool:
        """Whether ``agent`` may fetch ``url`` under these rules (True when there is no
        robots.txt)."""
        return True if not self.exists else bool(self._parser().can_fetch(agent, url))

    def delay(self, agent: str = "*") -> float | None:
        """The ``Crawl-delay`` for ``agent`` in seconds, if the site declares one."""
        if not self.exists:
            return None
        d = self._parser().crawl_delay(agent)
        return float(d) if d is not None else None


class IWebClient(BaseModel):
    """The client's Core Fields (policy), plus (for the checker) the eager
    authoring verbs ``WebClient`` implements -- ``fetch`` / ``ref`` / ``summary``.
    The verbs are ``TYPE_CHECKING``-only, so at runtime this is just the policy
    model."""

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
    #: how this client's browser is launched (headless / stealth / fingerprint). Stealth
    #: is on by default; pass ``BrowserConfig(headless=False)`` to drive a visible
    #: browser or ``BrowserConfig.auto()`` for a randomised fingerprint per page.
    browser_config: BrowserConfig = BrowserConfig()

    if TYPE_CHECKING:
        # >>> generated: WebClient interface <<<
        # fmt: off
        def fetch(self, url: Any, *, browser: "bool | Literal['never', 'auto', 'always']" = ..., optional: bool = ..., error: Any = ..., keep_alive: 'bool | float' = ..., wait: 'WaitConfig | None' = ..., **kw: Any) -> "Document": ...
        def ref(self, url: Any, method: str = ..., **kw: Any) -> "Reference": ...
        def robots(self, url: Any) -> "Robots": ...
        def sitemap(self, url: Any, *, limit: int = ...) -> "Collection[Reference]": ...
        # fmt: on
        # >>> end generated <<<
        pass


__all__ = ["IWebClient", "Robots"]
