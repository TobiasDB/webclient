"""One place for the useful configurable options -- a pydantic :class:`Settings`
that composes the transport / browser / resiliency / LLM config the client and the
pipeline already use, loadable from the environment and buildable into a configured
``WebClient`` / ``LlmClient``.

    settings = Settings.from_env()          # WEBCLIENT_* env vars
    wc = settings.client()                  # a configured WebClient
    llm = settings.llm_client()             # a configured LlmClient (auth from env)

Kept dependency-free (a plain ``BaseModel`` + an explicit env reader) rather than
pulling in ``pydantic-settings``; the config sub-models (``BrowserConfig`` /
``Resolve``) are the very ones the client already takes.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from .core.reference.models import BrowserConfig, Resolve

if TYPE_CHECKING:
    from .pipelines.llm import LlmClient
    from .surfaces import WebClient


def _env_bool(value: str) -> bool:
    return value.strip().lower() in ("1", "true", "yes", "on")


class LlmSettings(BaseModel):
    """LLM configuration (the API key is read from the environment, never stored)."""

    model: str = "claude-opus-5"
    base_url: str | None = None  # any Messages-API endpoint; None = Anthropic
    max_tokens: int = 4096
    budget_usd: float | None = None  # cap total spend; None = uncapped


class Settings(BaseModel):
    """Every useful knob in one object: transport (timeout / retries / politeness /
    SSRF guard / headers), the resiliency ``resolve`` bundle, the ``browser`` launch
    config, and the ``llm`` config. Build it explicitly, or with :meth:`from_env`;
    turn it into a configured :meth:`client` / :meth:`llm_client`."""

    model_config = {"arbitrary_types_allowed": True}

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
    def from_env(cls, prefix: str = "WEBCLIENT_", env: "dict[str, str] | None" = None) -> "Settings":
        """Build from environment variables (``WEBCLIENT_*`` by default): ``TIMEOUT``,
        ``RETRIES``, ``RETRY_BACKOFF``, ``MIN_INTERVAL``, ``BLOCK_PRIVATE_HOSTS``;
        ``BROWSER_HEADLESS`` / ``BROWSER_STEALTH`` / ``BROWSER_FINGERPRINT``;
        ``LLM_MODEL`` / ``LLM_BASE_URL`` / ``LLM_MAX_TOKENS`` / ``LLM_BUDGET_USD``.
        Anything unset keeps its default. (The LLM API key stays in
        ``ANTHROPIC_API_KEY`` -- it is not part of Settings.)"""
        e = os.environ if env is None else env

        def get(name: str) -> str | None:
            return e.get(prefix + name)

        s = cls()
        if (v := get("TIMEOUT")) is not None:
            s.timeout = float(v)
        if (v := get("RETRIES")) is not None:
            s.retries = int(v)
        if (v := get("RETRY_BACKOFF")) is not None:
            s.retry_backoff = float(v)
        if (v := get("MIN_INTERVAL")) is not None:
            s.min_interval = float(v)
        if (v := get("BLOCK_PRIVATE_HOSTS")) is not None:
            s.block_private_hosts = _env_bool(v)
        bkw: dict[str, Any] = {}
        if (v := get("BROWSER_HEADLESS")) is not None:
            bkw["headless"] = _env_bool(v)
        if (v := get("BROWSER_STEALTH")) is not None:
            bkw["stealth"] = _env_bool(v)
        if (v := get("BROWSER_FINGERPRINT")) is not None:
            bkw["fingerprint"] = _env_bool(v)
        if bkw:
            s.browser = s.browser.model_copy(update=bkw)
        if (v := get("LLM_MODEL")) is not None:
            s.llm.model = v
        if (v := get("LLM_BASE_URL")) is not None:
            s.llm.base_url = v
        if (v := get("LLM_MAX_TOKENS")) is not None:
            s.llm.max_tokens = int(v)
        if (v := get("LLM_BUDGET_USD")) is not None:
            s.llm.budget_usd = float(v)
        return s

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
        from .pipelines.llm import Budget, LlmClient

        kwargs: dict[str, Any] = {
            "model": self.llm.model,
            "base_url": self.llm.base_url,
            "max_tokens": self.llm.max_tokens,
            "auth": auth,
            "budget": Budget(max_usd=self.llm.budget_usd),
        }
        kwargs.update(overrides)
        return LlmClient(**kwargs)


__all__ = ["Settings", "LlmSettings"]
