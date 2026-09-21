"""One place for the configurable options -- a :class:`Settings` (a
``pydantic_settings.BaseSettings``) composing the transport / browser / resiliency /
LLM config the client and the pipeline use, plus the package's tunable LIMITS, loaded from
the environment and buildable into a configured ``WebClient`` / ``LlmClient``.

    settings = Settings()                   # reads WEBCLIENT_* env vars
    wc = settings.client()                  # a configured WebClient
    llm = settings.llm_client()             # a configured LlmClient (auth from env)
    settings.configure_logging()            # honour WEBCLIENT_LOG_LEVEL

Env vars are ``WEBCLIENT_``-prefixed; nested fields use a ``__`` delimiter --
``WEBCLIENT_TIMEOUT``, ``WEBCLIENT_BROWSER__HEADLESS``, ``WEBCLIENT_LLM__MODEL``,
``WEBCLIENT_LIMITS__CHATTY_ROUND_TRIPS``, ``WEBCLIENT_DETECTION__PRESENT_THRESHOLD``.

**The rule** (roadmap N1): a literal a user might reasonably want to change is a setting
and lives here; a literal that encodes a spec (HTTP retriable statuses, tag names) stays
in its module. Modules read the process-wide :func:`current` settings at USE time (never
at import), so ``use(Settings(...))`` re-tunes a running process and tests can override.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict

from .policy import BrowserConfig, Resolve

if TYPE_CHECKING:
    from .clients.llm import LlmClient
    from .interface import WebClient


class LlmSettings(BaseModel):
    """LLM configuration (the API key is read from the environment, never stored)."""

    model_config = {"arbitrary_types_allowed": True}

    model: str = "claude-opus-5"
    base_url: str | None = None  # any Messages-API endpoint; None = Anthropic
    max_tokens: int = 4096
    budget_usd: float | None = None  # cap total spend; None = uncapped
    min_interval: float = 0.0  # client-side rate limit: min seconds between LLM calls
    max_retries: int = 4  # retry a 429 / 5xx / 529 with backoff
    #: per-model price overrides (prices change) -- merged over the default table when
    #: building the client, e.g. ``{"claude-opus-5": ModelPrice.of(6, 30)}``.
    pricing: dict[str, Any] = {}


class LimitsSettings(BaseModel):
    """Size / count / time caps that bound the package's hot paths."""

    chatty_round_trips: int = 4  # per-op remote round-trips before nudging toward .lazy
    json_leaf_budget: int = 20000  # scalar leaves walked from one JSON value
    skeleton_max_lines: int = 400  # default line cap of a DOM / JSON skeleton
    pool_acquire_timeout: float = 60.0  # seconds to wait for a transport lease
    engine_stop_timeout: float = 5.0  # seconds to drain the engine loop on close
    host_schedule_max: int = 4096  # per-host politeness entries kept before pruning


class DetectionSettings(BaseModel):
    """Signals -> flags tuning."""

    present_threshold: float = 0.5  # a flag's confidence must reach this to be "present"


class ServiceSettings(BaseModel):
    """The HTTP service's resource caps (``webclient.service.create_app`` defaults)."""

    max_docs: int = 1024  # shared LRU document handles
    max_sessions: int = 256  # live server-side sessions
    max_session_docs: int = 256  # document handles EACH session may hold


class LoopSettings(BaseModel):
    """Default budgets for the bounded loops (crawl has its own ``CrawlConfig``)."""

    max_rounds: int = 20  # BoundedLoop round budget
    max_stalls: int = 3  # consecutive no-progress rounds before "stalled"
    query_max_rounds: int = 4  # the query-authoring loop's budget


class Settings(BaseSettings):
    """Every useful knob in one object: transport (timeout / retries / politeness /
    SSRF guard / headers), the resiliency ``resolve`` bundle, the ``browser`` launch
    config, the ``llm`` config, and the package limits. Constructing it reads the
    ``WEBCLIENT_*`` environment (init kwargs win); turn it into a configured
    :meth:`client` / :meth:`llm_client`, or install it process-wide with :func:`use`."""

    model_config = SettingsConfigDict(
        env_prefix="WEBCLIENT_",
        env_nested_delimiter="__",
        arbitrary_types_allowed=True,
        extra="ignore",
    )

    # -- transport / client --------------------------------------------------
    timeout: float = 30.0
    retries: int = 0
    retry_backoff: float = 0.2
    min_interval: float = 0.0  # per-host politeness (seconds between requests)
    block_private_hosts: bool = False  # SSRF guard
    default_headers: dict[str, str] = {}
    resolve: Resolve | None = None  # retry / rate / proxy / antibot / browser policies
    browser: BrowserConfig = BrowserConfig()  # headless / stealth / fingerprint
    # -- llm -----------------------------------------------------------------
    llm: LlmSettings = LlmSettings()
    # -- package tuning ------------------------------------------------------
    limits: LimitsSettings = LimitsSettings()
    detection: DetectionSettings = DetectionSettings()
    service: ServiceSettings = ServiceSettings()
    loops: LoopSettings = LoopSettings()
    # -- logging -------------------------------------------------------------
    log_level: str | None = None  # e.g. "DEBUG"; None = leave logging unconfigured

    @classmethod
    def from_env(cls) -> "Settings":
        """The environment-loaded settings (an alias for ``Settings()``, which reads
        ``WEBCLIENT_*`` itself)."""
        return cls()

    # -- builders ------------------------------------------------------------
    def client(self, **overrides: Any) -> "WebClient":
        """A ``WebClient`` configured from these settings (per-call ``overrides`` win)."""
        from .interface import WebClient

        kwargs: dict[str, Any] = {
            "timeout": self.timeout,
            "retries": self.retries,
            "retry_backoff": self.retry_backoff,
            "min_interval": self.min_interval,
            "block_private_hosts": self.block_private_hosts,
            "default_headers": dict(self.default_headers),
            "resolve": self.resolve,
            "browser_config": self.browser,
        }
        kwargs.update(overrides)
        return WebClient(**kwargs)

    def llm_client(self, *, auth: str | None = None, **overrides: Any) -> "LlmClient":
        """An ``LlmClient`` configured from ``self.llm`` (auth from ``auth`` or the
        environment's ``ANTHROPIC_API_KEY``; the budget from ``llm.budget_usd``)."""
        from .clients.llm import PRICING, Budget, LlmClient

        kwargs: dict[str, Any] = {
            "model": self.llm.model,
            "base_url": self.llm.base_url,
            "max_tokens": self.llm.max_tokens,
            "auth": auth,
            "budget": Budget(max_usd=self.llm.budget_usd),
            "pricing": {**PRICING, **self.llm.pricing},  # price overrides win
            "min_interval": self.llm.min_interval,  # rate limit
            "max_retries": self.llm.max_retries,
        }
        kwargs.update(overrides)
        return LlmClient(**kwargs)

    def configure_logging(self) -> "logging.Logger":
        """Attach a stream handler at ``log_level`` (no-op when it is ``None``)."""
        from .log import configure_logging

        return configure_logging(self.log_level)


# -- the process-wide settings the modules read at use time --------------------
_CURRENT: "Settings | None" = None


def current() -> Settings:
    """The process-wide settings: the last :func:`use`, else one loaded from the environment
    on first call. Modules read their tunables through this (``current().limits.…``) at USE
    time, so they never freeze a value at import."""
    global _CURRENT
    if _CURRENT is None:
        _CURRENT = Settings()
    return _CURRENT


def use(settings: "Settings | None") -> "Settings | None":
    """Install ``settings`` as the process-wide settings (``None`` resets to environment
    loading on the next :func:`current`). Returns the previous value so a test can restore."""
    global _CURRENT
    previous = _CURRENT
    _CURRENT = settings
    return previous


__all__ = [
    "Settings", "LlmSettings", "LimitsSettings", "DetectionSettings", "ServiceSettings",
    "LoopSettings", "current", "use",
]
