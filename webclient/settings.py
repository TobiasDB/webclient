"""One place for the useful configurable options -- a :class:`Settings` (a
``pydantic_settings.BaseSettings``) composing the transport / browser / resiliency /
LLM config the client and the pipeline already use, loaded from the environment and
buildable into a configured ``WebClient`` / ``LlmClient``.

    settings = Settings()                   # reads WEBCLIENT_* env vars
    wc = settings.client()                  # a configured WebClient
    llm = settings.llm_client()             # a configured LlmClient (auth from env)

Env vars are ``WEBCLIENT_``-prefixed; nested fields use a ``__`` delimiter --
``WEBCLIENT_TIMEOUT``, ``WEBCLIENT_BROWSER__HEADLESS``, ``WEBCLIENT_LLM__MODEL``,
``WEBCLIENT_LLM__BUDGET_USD``. The config sub-models (``BrowserConfig`` / ``Resolve``)
are the very ones the client already takes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict

from .core.reference.models import BrowserConfig, Resolve

if TYPE_CHECKING:
    from .pipelines.llm import LlmClient
    from .surfaces import WebClient


class LlmSettings(BaseModel):
    """LLM configuration (the API key is read from the environment, never stored)."""

    model_config = {"arbitrary_types_allowed": True}

    model: str = "claude-opus-5"
    base_url: str | None = None  # any Messages-API endpoint; None = Anthropic
    max_tokens: int = 4096
    budget_usd: float | None = None  # cap total spend; None = uncapped
    #: per-model price overrides (prices change) -- merged over the default table when
    #: building the client, e.g. ``{"claude-opus-5": ModelPrice.of(6, 30)}``.
    pricing: dict[str, Any] = {}


class Settings(BaseSettings):
    """Every useful knob in one object: transport (timeout / retries / politeness /
    SSRF guard / headers), the resiliency ``resolve`` bundle, the ``browser`` launch
    config, and the ``llm`` config. Constructing it reads the ``WEBCLIENT_*``
    environment (init kwargs win); turn it into a configured :meth:`client` /
    :meth:`llm_client`."""

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

    @classmethod
    def from_env(cls) -> "Settings":
        """The environment-loaded settings (an alias for ``Settings()``, which reads
        ``WEBCLIENT_*`` itself)."""
        return cls()

    # -- builders ------------------------------------------------------------
    def client(self, **overrides: Any) -> "WebClient":
        """A ``WebClient`` configured from these settings (per-call ``overrides`` win)."""
        from .surfaces import WebClient

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
        from .pipelines.llm import PRICING, Budget, LlmClient

        kwargs: dict[str, Any] = {
            "model": self.llm.model,
            "base_url": self.llm.base_url,
            "max_tokens": self.llm.max_tokens,
            "auth": auth,
            "budget": Budget(max_usd=self.llm.budget_usd),
            "pricing": {**PRICING, **self.llm.pricing},  # price overrides win
        }
        kwargs.update(overrides)
        return LlmClient(**kwargs)


__all__ = ["Settings", "LlmSettings"]
