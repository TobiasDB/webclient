"""Standardised env-var configuration + the default builders the programmatic entries and the CLI
share.

Every operational knob is a ``WEB_*`` env var; an explicit argument (or CLI flag) always overrides
it. LLM creds also honour the well-known ``ANTHROPIC_API_KEY`` / ``ANTHROPIC_BASE_URL`` (``WEB_LLM_*``
wins). The full set:

A ``.env`` file (repo root, or ``WEB_ENV_FILE``) is loaded on import to seed these; a real shell
variable always overrides the file.

======================  ==========================================================================
  WEB_ENV_FILE          path to the ``.env`` to load (else the nearest ``.env`` up from the cwd)
  WEB_LLM_SHIM          use the local ``claude -p`` shim (no API key)                       [bool]
  WEB_LLM_MODEL         model id / CLI alias (e.g. ``haiku``, ``claude-sonnet-5``)
  WEB_LLM_API_KEY       Anthropic API key                             (else ``ANTHROPIC_API_KEY``)
  WEB_LLM_BASE_URL      Anthropic base URL                            (else ``ANTHROPIC_BASE_URL``)
  WEB_LLM_RATE          minimum seconds between LLM calls                                  [float]
  WEB_LLM_TIMEOUT       per-call timeout for the shim                                      [float]
  WEB_AUTHOR_ENGINE     how the author writes the query: ``steps`` (default) / ``chain``
  WEB_PRICE_INPUT       spend report: input price ($/million tokens)                       [float]
  WEB_PRICE_OUTPUT      spend report: output price ($/M tokens)                            [float]
  WEB_PRICE_CACHE_READ  spend report: cache-read price ($/M tokens)                        [float]
  WEB_PRICE_CACHE_WRITE spend report: cache-write price ($/M tokens)                       [float]
  WEB_PROFILE           resolve profile: ``basic`` / ``basic_browser`` / ``full_browser``
  WEB_PROXY             proxy URL for all traffic (``http://[user:pass@]host:port``)
  WEB_BROWSER_PATH      an explicit browser binary for every browser tier
======================  ==========================================================================
"""

from __future__ import annotations

import os
from pathlib import Path

from web.fetch import Profile as FetchProfile
from web.resolve import EscalationPolicy, Resolver
from web.resolve import profiles as _rp

from .llm import AnthropicLlm, Llm
from .locate import Search
from .search import DdgSearch
from .shim import ClaudeShim


def _find_env_file() -> "Path | None":
    """The ``.env`` to load: ``WEB_ENV_FILE`` when set, else the nearest ``.env`` walking up from
    the cwd (repo-root ``.env`` is found from any package dir). ``None`` when there is none."""
    explicit = os.environ.get("WEB_ENV_FILE")
    if explicit:
        p = Path(explicit)
        return p if p.is_file() else None
    here = Path.cwd()
    for folder in (here, *here.parents):
        cand = folder / ".env"
        if cand.is_file():
            return cand
    return None


def load_dotenv(path: "str | Path | None" = None, *, override: bool = False) -> "str | None":
    """Load ``KEY=VALUE`` lines from a ``.env`` file into ``os.environ`` and return the file used
    (``None`` if none). ``#`` comments, blank lines, an ``export`` prefix and single/double quotes
    around the value are all handled. A REAL environment variable WINS over the file unless
    ``override`` -- so ``.env`` supplies defaults you can still override from the shell."""
    target = Path(path) if path is not None else _find_env_file()
    if target is None or not target.is_file():
        return None
    try:
        text = target.read_text(encoding="utf-8")
    except OSError:
        return None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, sep, value = line.partition("=")
        key = key.strip()
        if not sep or not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if override or key not in os.environ:
            os.environ[key] = value
    return str(target)


#: Load a ``.env`` on import so every ``WEB_*`` read below (and in the clients) sees it; the real
#: environment always wins. ``WEB_ENV_FILE`` points at a specific file; otherwise the nearest ``.env``.
load_dotenv()


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
    # WEB_LLM_RATE is applied by the clients themselves (RateLimit.from_env via their gate), so it
    # holds for a directly-built AnthropicLlm()/ClaudeShim() too, not only through here.
    use_shim = env_flag("WEB_LLM_SHIM") if shim is None else shim
    return ClaudeShim(model=model) if use_shim else AnthropicLlm(model=model)


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


__all__ = [
    "default_llm",
    "default_search",
    "build_resolver",
    "load_dotenv",
    "env",
    "env_flag",
    "env_float",
]
