"""A small, reusable LLM client for the pipelines -- and the cost budget that caps a
run.

The pipeline injects the model as a plain callable (:data:`LLM`, a
``Callable[[str], str]``): a prompt in, its completion text out. :class:`LlmClient`
*is* such a callable, so ``onboard_company(..., llm=LlmClient(...))`` just works while
the stub-``llm`` used in tests keeps working unchanged.

The client is deliberately dependency-light -- it speaks the Anthropic Messages API
over ``httpx`` (already a dependency), with no SDK. It defaults to Anthropic but keeps
``base_url`` overridable, so any OpenAI-compatible / Messages-API gateway can be
pointed at it. Every call reads the response ``usage`` and charges a :class:`Budget`;
when a configured cap is exceeded the next call raises :class:`BudgetExceeded`.

Testing offline: pass an ``httpx.MockTransport`` (or your own ``httpx.Client``) so no
network is touched -- see ``tests/test_onboarding.py``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

import httpx

#: The Anthropic Messages API, the default provider.
DEFAULT_BASE_URL = "https://api.anthropic.com"
#: The default model. Kept current with the packaged ``claude-api`` skill.
DEFAULT_MODEL = "claude-opus-5"
#: The Messages API version header value.
ANTHROPIC_VERSION = "2023-06-01"


# --------------------------------------------------------------------------- #
# Pricing + token usage -> USD cost.
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ModelPrice:
    """USD price per 1,000,000 tokens, split by direction."""

    input_usd_per_mtok: float
    output_usd_per_mtok: float


#: Per-model USD prices per 1M tokens (input, output), sourced from the packaged
#: ``claude-api`` skill (2026-06). Unknown models fall back to :data:`_FALLBACK_PRICE`.
PRICING: dict[str, ModelPrice] = {
    "claude-fable-5-1": ModelPrice(10.0, 50.0),
    "claude-fable-5": ModelPrice(10.0, 50.0),
    "claude-opus-5": ModelPrice(5.0, 25.0),
    "claude-opus-4-8": ModelPrice(5.0, 25.0),
    "claude-opus-4-7": ModelPrice(5.0, 25.0),
    "claude-opus-4-6": ModelPrice(5.0, 25.0),
    "claude-sonnet-5": ModelPrice(2.0, 10.0),
    "claude-sonnet-4-6": ModelPrice(3.0, 15.0),
    "claude-haiku-4-5": ModelPrice(1.0, 5.0),
}
#: Used when a model id is not in :data:`PRICING` (assume Opus-tier so we never
#: under-count spend against a budget).
_FALLBACK_PRICE = ModelPrice(5.0, 25.0)

# Cache multipliers relative to the input price (Anthropic prompt caching).
_CACHE_WRITE_MULT = 1.25
_CACHE_READ_MULT = 0.10


def price_for(model: str) -> ModelPrice:
    """The :class:`ModelPrice` for ``model`` (Opus-tier fallback if unknown)."""
    return PRICING.get(model, _FALLBACK_PRICE)


@dataclass(frozen=True)
class Usage:
    """The token counts from one Messages API response."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    @classmethod
    def from_response(cls, usage: dict[str, Any]) -> "Usage":
        """Parse the ``usage`` object of a Messages API response (missing -> 0)."""
        def _n(key: str) -> int:
            return int(usage.get(key) or 0)

        return cls(
            input_tokens=_n("input_tokens"),
            output_tokens=_n("output_tokens"),
            cache_read_tokens=_n("cache_read_input_tokens"),
            cache_write_tokens=_n("cache_creation_input_tokens"),
        )

    def cost_usd(self, price: ModelPrice) -> float:
        """This usage priced in USD, cached tokens included at their reduced rate."""
        mtok = 1_000_000.0
        return (
            self.input_tokens * price.input_usd_per_mtok
            + self.cache_write_tokens * price.input_usd_per_mtok * _CACHE_WRITE_MULT
            + self.cache_read_tokens * price.input_usd_per_mtok * _CACHE_READ_MULT
            + self.output_tokens * price.output_usd_per_mtok
        ) / mtok


# --------------------------------------------------------------------------- #
# Budget: track running spend, and (optionally) cap it.
# --------------------------------------------------------------------------- #


class BudgetExceeded(RuntimeError):
    """Raised when an LLM call would run while the budget is already spent out.

    Carries the running ``spent_usd`` and the ``limit_usd`` that was crossed so a
    caller can report the overrun.
    """

    def __init__(self, spent_usd: float, limit_usd: float) -> None:
        self.spent_usd = spent_usd
        self.limit_usd = limit_usd
        super().__init__(
            f"LLM budget exceeded: spent ${spent_usd:.4f} of ${limit_usd:.4f} cap"
        )


@dataclass
class Budget:
    """Running LLM spend, in USD, with an optional hard cap.

    A :class:`Budget` both *accumulates* spend (always) and, when ``max_usd`` is set,
    *enforces* it: once the accumulated spend reaches the cap the next
    :meth:`ensure` -- called before each LLM request -- raises :class:`BudgetExceeded`,
    so a run stops cleanly instead of racking up further cost. ``max_usd=None`` tracks
    spend without capping it.
    """

    max_usd: float | None = None
    spent_usd: float = 0.0
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0

    def ensure(self) -> None:
        """Raise :class:`BudgetExceeded` if the cap has already been reached."""
        if self.max_usd is not None and self.spent_usd >= self.max_usd:
            raise BudgetExceeded(self.spent_usd, self.max_usd)

    def charge(self, usage: Usage, price: ModelPrice) -> float:
        """Record one call's ``usage`` against the running spend; return its cost."""
        cost = usage.cost_usd(price)
        self.spent_usd += cost
        self.calls += 1
        self.input_tokens += usage.input_tokens
        self.output_tokens += usage.output_tokens
        return cost

    @property
    def remaining_usd(self) -> float | None:
        """USD left before the cap (``None`` when uncapped); never below 0."""
        if self.max_usd is None:
            return None
        return max(0.0, self.max_usd - self.spent_usd)


# --------------------------------------------------------------------------- #
# The client.
# --------------------------------------------------------------------------- #


@dataclass
class LlmClient:
    """A reusable, dependency-light LLM client that is a drop-in :data:`LLM`.

    Call it with a prompt string to get the completion text back. Configure the
    ``model``, the ``base_url`` (any Messages-API endpoint), and ``auth`` (an API key /
    bearer token) explicitly, or leave them to fall back to ``ANTHROPIC_BASE_URL`` /
    ``ANTHROPIC_API_KEY``. Attach a :class:`Budget` to cap and track spend.

    Offline testing: pass ``transport`` (an ``httpx.MockTransport``) or a fully-formed
    ``http_client``; either keeps the client from touching the network.
    """

    model: str = DEFAULT_MODEL
    base_url: str | None = None
    auth: str | None = None
    max_tokens: int = 4096
    system: str | None = None
    timeout: float = 60.0
    budget: Budget = field(default_factory=Budget)
    transport: httpx.BaseTransport | None = None
    http_client: httpx.Client | None = None
    anthropic_version: str = ANTHROPIC_VERSION
    #: The token usage of the most recent call (``None`` before the first).
    last_usage: Usage | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        base = self.base_url or os.environ.get("ANTHROPIC_BASE_URL") or DEFAULT_BASE_URL
        self.base_url = base.rstrip("/")
        if self.auth is None:
            self.auth = os.environ.get("ANTHROPIC_API_KEY")
        if self.http_client is None:
            self.http_client = httpx.Client(
                timeout=self.timeout, transport=self.transport
            )

    # -- the LLM protocol: a prompt in, its completion text out ---------------- #

    def __call__(self, prompt: str) -> str:
        """Complete ``prompt`` -- charging the budget and enforcing its cap."""
        self.budget.ensure()  # stop before spending past the cap
        text, usage = self._complete(prompt)
        self.last_usage = usage
        self.budget.charge(usage, price_for(self.model))
        return text

    def _complete(self, prompt: str) -> tuple[str, Usage]:
        payload: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }
        if self.system is not None:
            payload["system"] = self.system
        headers = {
            "content-type": "application/json",
            "anthropic-version": self.anthropic_version,
        }
        if self.auth:
            headers["x-api-key"] = self.auth
        assert self.http_client is not None  # set in __post_init__
        resp = self.http_client.post(
            f"{self.base_url}/v1/messages", json=payload, headers=headers
        )
        resp.raise_for_status()
        data: dict[str, Any] = resp.json()
        text = "".join(
            str(block.get("text", ""))
            for block in data.get("content", [])
            if isinstance(block, dict) and block.get("type") == "text"
        )
        return text, Usage.from_response(data.get("usage") or {})

    # -- running spend --------------------------------------------------------- #

    @property
    def spent_usd(self) -> float:
        """Total USD spent through this client's budget so far."""
        return self.budget.spent_usd

    def close(self) -> None:
        """Close the underlying HTTP client."""
        if self.http_client is not None:
            self.http_client.close()

    def __enter__(self) -> "LlmClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


__all__ = [
    "LlmClient",
    "Budget",
    "BudgetExceeded",
    "Usage",
    "ModelPrice",
    "PRICING",
    "price_for",
    "DEFAULT_MODEL",
    "DEFAULT_BASE_URL",
]
