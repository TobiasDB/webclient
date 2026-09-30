"""Standardised env-var configuration + the default builders the programmatic entries and the CLI
share.

Every operational knob is a ``WEB_*`` env var; an explicit argument (or CLI flag) always overrides
it. LLM creds also honour the well-known ``ANTHROPIC_API_KEY`` / ``ANTHROPIC_BASE_URL`` (``WEB_LLM_*``
wins). The full set:

======================  ==========================================================================
  WEB_LLM_SHIM          use the local ``claude -p`` shim (no API key)                       [bool]
  WEB_LLM_MODEL         model id / CLI alias (e.g. ``haiku``, ``claude-sonnet-5``)
  WEB_LLM_API_KEY       Anthropic API key                             (else ``ANTHROPIC_API_KEY``)
  WEB_LLM_BASE_URL      Anthropic base URL                            (else ``ANTHROPIC_BASE_URL``)
  WEB_LLM_RATE          minimum seconds between LLM calls                                  [float]
  WEB_LLM_TIMEOUT       per-call timeout for the shim                                      [float]
  WEB_PROFILE           resolve profile: ``basic`` / ``basic_browser`` / ``full_browser``
  WEB_PROXY             proxy URL for all traffic (``http://[user:pass@]host:port``)
  WEB_BROWSER_PATH      an explicit browser binary for every browser tier
======================  ==========================================================================
"""

from __future__ import annotations

import os

from web.fetch import Profile as FetchProfile
from web.resolve import EscalationPolicy, Resolver
from web.resolve import profiles as _rp

from .llm import AnthropicLlm, Llm, RateLimit
from .locate import Search
from .search import DdgSearch
from .shim import ClaudeShim


def env(name: str, default: "str | None" = None) -> "str | None":
    """A ``WEB_*`` override (empty string counts as unset)."""
    return os.environ.get(name) or default


def env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def env_float(name: str, default: float) -> float:
    try:
        return float(os.environ[name])
    except (KeyError, ValueError):
        return default


def default_llm(*, model: "str | None" = None, shim: "bool | None" = None) -> Llm:
    """The LLM from the env. ``WEB_LLM_SHIM`` -> the local ``claude -p`` shim (no key, real model);
    else the Anthropic API (:class:`AnthropicLlm` reads its key / base_url / model from the env
    itself). ``shim`` / ``model`` override the env."""
    use_shim = env_flag("WEB_LLM_SHIM") if shim is None else shim
    if use_shim:
        return ClaudeShim(model=model)
    rate = env_float("WEB_LLM_RATE", 0.0)
    return AnthropicLlm(model=model, rate=RateLimit(min_interval=rate) if rate else None)


def default_search() -> Search:
    """The default web-search backend (DuckDuckGo); inject your own :class:`Search` to replace it."""
    return DdgSearch()


def build_resolver(
    *,
    profile: "str | None" = None,
    proxy: "str | None" = None,
    browser_path: "str | None" = None,
    pool: "object | None" = None,
) -> Resolver:
    """A resolver from a named profile (``WEB_PROFILE`` when unset), routed through ``proxy``
    (``WEB_PROXY``) and pinning ``browser_path`` (``WEB_BROWSER_PATH``, also read by the fetch layer)
    onto every browser tier. ``basic`` is HTTP; ``basic_browser`` escalates on a block; ``full_browser``
    always renders."""
    name = profile or env("WEB_PROFILE", "basic_browser") or "basic_browser"
    px = proxy or env("WEB_PROXY")
    if px:
        factory = {
            "basic": _rp.proxy,
            "basic_browser": _rp.proxy_browser,
            "full_browser": _rp.proxy_full_browser,
        }.get(name, _rp.proxy_browser)
        prof = factory(px)
    else:
        got = _rp.get(name)
        if got is None:
            return Resolver(pool=pool)  # type: ignore[arg-type]
        prof = got
    bp = browser_path or env("WEB_BROWSER_PATH")
    if bp and prof.escalation:  # pin the binary on every browser tier (fetch also honours the env)
        tiers = tuple(
            t.with_(executable_path=bp) if isinstance(t, FetchProfile) and t.browser else t
            for t in prof.escalation.tiers
        )
        prof = prof.with_(escalation=EscalationPolicy(tiers=tiers, on=prof.escalation.on))
    return Resolver(profile=prof, pool=pool)  # type: ignore[arg-type]


__all__ = ["default_llm", "default_search", "build_resolver", "env", "env_flag", "env_float"]
