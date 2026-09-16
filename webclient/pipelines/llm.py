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
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

#: statuses worth retrying: rate limit (429), transient server / overload errors.
_RETRIABLE_STATUS = frozenset({429, 500, 502, 503, 504, 529})


def _error_message(resp: "httpx.Response") -> str:
    """The API's error message (Anthropic sends ``{"error": {"type", "message"}}``),
    falling back to the raw body -- so a 400 tells you WHY (prompt too long, etc.)."""
    try:
        err = resp.json().get("error") or {}
        msg = err.get("message") or ""
        return f"{err.get('type', '')}: {msg}".strip(": ") or resp.text[:300]
    except Exception:
        return resp.text[:300]


class LlmError(RuntimeError):
    """A non-retriable (or retry-exhausted) LLM API error. Carries the HTTP
    ``status_code`` and the API's ``message`` -- a 400 usually says WHY (e.g. the
    prompt is too long), so it is surfaced rather than swallowed."""

    def __init__(self, status_code: int, message: str) -> None:
        self.status_code = status_code
        self.message = message
        super().__init__(f"LLM API {status_code}: {message}")


#: The Anthropic Messages API, the default provider.
DEFAULT_BASE_URL = "https://api.anthropic.com"
#: The default model. Kept current with the packaged ``claude-api`` skill.
DEFAULT_MODEL = "claude-opus-5"
#: The Messages API version header value.
ANTHROPIC_VERSION = "2023-06-01"


# --------------------------------------------------------------------------- #
# Pricing + token usage -> USD cost.
# --------------------------------------------------------------------------- #


# Anthropic prompt-cache pricing, relative to the base input price: a cache WRITE
# costs ~1.25x input (a 5-minute cache), a cache READ ~0.10x. Kept as the defaults
# ``ModelPrice.of`` fills in, so an explicit per-model / per-TTL override is possible.
_CACHE_WRITE_MULT = 1.25
_CACHE_READ_MULT = 0.10


@dataclass(frozen=True)
class ModelPrice:
    """USD price per 1,000,000 tokens, one field per billed direction: base ``input`` /
    ``output`` plus prompt-cache ``cache_write`` (writing tokens into the cache) and
    ``cache_read`` (reading a cache hit, much cheaper). Build it with :meth:`of` to
    derive the cache prices from the input price, or set all four explicitly."""

    input_usd_per_mtok: float
    output_usd_per_mtok: float
    cache_write_usd_per_mtok: float
    cache_read_usd_per_mtok: float

    @classmethod
    def of(
        cls,
        input_usd_per_mtok: float,
        output_usd_per_mtok: float,
        *,
        cache_write_mult: float = _CACHE_WRITE_MULT,
        cache_read_mult: float = _CACHE_READ_MULT,
    ) -> "ModelPrice":
        """A price with the cache write/read derived from the input price (the usual
        Anthropic ratios); pass the multipliers to override (e.g. a 1-hour cache)."""
        return cls(
            input_usd_per_mtok,
            output_usd_per_mtok,
            input_usd_per_mtok * cache_write_mult,
            input_usd_per_mtok * cache_read_mult,
        )


#: Per-model USD prices per 1M tokens (input, output, + derived cache write/read),
#: sourced from the packaged ``claude-api`` skill (2026-06). Unknown models fall back
#: to :data:`_FALLBACK_PRICE`.
PRICING: dict[str, ModelPrice] = {
    "claude-fable-5-1": ModelPrice.of(10.0, 50.0),
    "claude-fable-5": ModelPrice.of(10.0, 50.0),
    "claude-opus-5": ModelPrice.of(5.0, 25.0),
    "claude-opus-4-8": ModelPrice.of(5.0, 25.0),
    "claude-opus-4-7": ModelPrice.of(5.0, 25.0),
    "claude-opus-4-6": ModelPrice.of(5.0, 25.0),
    "claude-sonnet-5": ModelPrice.of(2.0, 10.0),
    "claude-sonnet-4-6": ModelPrice.of(3.0, 15.0),
    "claude-haiku-4-5": ModelPrice.of(1.0, 5.0),
}
#: Used when a model id is not in :data:`PRICING` (assume Opus-tier so we never
#: under-count spend against a budget).
_FALLBACK_PRICE = ModelPrice.of(5.0, 25.0)


def price_for(model: str) -> ModelPrice:
    """The :class:`ModelPrice` for ``model`` (Opus-tier fallback if unknown)."""
    return PRICING.get(model, _FALLBACK_PRICE)


def cheapest_model() -> str:
    """The cheapest model in :data:`PRICING`, by input+output price per 1M tokens -- so a
    cost-sensitive run (a test harness, a smoke eval) can pick the least-expensive model
    without hard-coding an id that a price update might dethrone."""
    return min(
        PRICING,
        key=lambda m: PRICING[m].input_usd_per_mtok + PRICING[m].output_usd_per_mtok,
    )


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
        """This usage priced in USD -- base input/output plus the cache write/read
        tokens at their own per-model rates."""
        mtok = 1_000_000.0
        return (
            self.input_tokens * price.input_usd_per_mtok
            + self.cache_write_tokens * price.cache_write_usd_per_mtok
            + self.cache_read_tokens * price.cache_read_usd_per_mtok
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
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

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
        self.cache_read_tokens += usage.cache_read_tokens
        self.cache_write_tokens += usage.cache_write_tokens
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
    #: retry a rate-limited (429) / transient server (5xx / 529) response this many
    #: times, with exponential backoff (and honouring a ``Retry-After`` header).
    max_retries: int = 4
    retry_backoff: float = 1.0  # base seconds, doubled each attempt
    #: a client-side rate limit: minimum seconds between requests (0 = none). Set it
    #: (or ``rpm``) to stay under the provider's limit instead of racking up 429s.
    min_interval: float = 0.0
    #: the price table this client charges against -- a copy of :data:`PRICING` by
    #: default, so a caller can override a model's price when it changes (prices are
    #: config, not a constant): ``LlmClient(pricing={**PRICING, "claude-opus-5":
    #: ModelPrice.of(6, 30)})``.
    pricing: dict[str, ModelPrice] = field(default_factory=lambda: dict(PRICING))
    transport: httpx.BaseTransport | None = None
    http_client: httpx.Client | None = None
    anthropic_version: str = ANTHROPIC_VERSION
    #: The token usage of the most recent call (``None`` before the first).
    last_usage: Usage | None = field(default=None, init=False)
    _last_call: float = field(default=0.0, init=False)  # for the min_interval rate limit

    def price(self) -> ModelPrice:
        """This client's price for its model (from :attr:`pricing`, Opus-tier fallback)."""
        return self.pricing.get(self.model, _FALLBACK_PRICE)

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
        """Complete ``prompt`` -- rate-limited, retried on a 429/5xx, budget-charged and
        budget-capped. A non-retriable error (e.g. a 400 for too-long a prompt) raises
        :class:`LlmError` carrying the API's message."""
        self.budget.ensure()  # stop before spending past the cap
        text, usage = self._complete(prompt)
        self.last_usage = usage
        self.budget.charge(usage, self.price())
        return text

    def _pace(self) -> None:
        """Enforce the ``min_interval`` rate limit -- sleep so requests are spaced."""
        if self.min_interval > 0:
            wait = self._last_call + self.min_interval - time.monotonic()
            if wait > 0:
                time.sleep(wait)
        self._last_call = time.monotonic()

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

        last: Exception | None = None
        for attempt in range(self.max_retries + 1):
            self._pace()  # rate limit before every attempt
            try:
                resp = self.http_client.post(
                    f"{self.base_url}/v1/messages", json=payload, headers=headers
                )
            except httpx.TransportError as exc:  # a dropped/refused connection -- retry
                last = exc
                if attempt < self.max_retries:
                    time.sleep(self._backoff(attempt))
                    continue
                raise LlmError(0, f"transport error: {exc}") from exc
            if 200 <= resp.status_code < 300:
                return self._parse(resp)
            # a retriable status (429 / 5xx / 529): back off (honour Retry-After) + retry
            if resp.status_code in _RETRIABLE_STATUS and attempt < self.max_retries:
                time.sleep(self._retry_after(resp) or self._backoff(attempt))
                continue
            # non-retriable (e.g. 400 bad request) or retries exhausted -- surface it
            raise LlmError(resp.status_code, _error_message(resp))
        raise LlmError(0, f"exhausted retries: {last}")  # pragma: no cover

    def _backoff(self, attempt: int) -> float:
        return self.retry_backoff * (2.0**attempt)

    @staticmethod
    def _retry_after(resp: "httpx.Response") -> float | None:
        value = resp.headers.get("retry-after")
        try:
            return min(float(value), 60.0) if value else None
        except ValueError:
            return None

    def _parse(self, resp: "httpx.Response") -> tuple[str, Usage]:
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
    "LlmError",
    "Usage",
    "ModelPrice",
    "PRICING",
    "price_for",
    "cheapest_model",
    "DEFAULT_MODEL",
    "DEFAULT_BASE_URL",
]
