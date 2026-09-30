"""The ``Llm`` protocol and an Anthropic Messages-API client over httpx.

The client is CONFIG-driven and self-metering: a :class:`RateLimit` (a minimum interval between
calls, so a shared/corporate key stays under its limit) and a :class:`Pricing` (per-token USD
prices for input / output / cache-read / cache-write) are passed in, and the client tracks
cumulative :class:`Usage` and spend from the API's own usage counts. Nothing here is vendor-locked
below :class:`Llm` -- swap in any ``complete(prompt) -> str``.
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from typing import Protocol, runtime_checkable

import httpx
from pydantic import BaseModel
from web.fetch import WebException, emit, err


def anticache_suffix() -> str:
    """A per-call ANTI-CACHE marker to append to every prompt. The author loop re-asks with the SAME
    page skeleton on each repair; a caching LLM gateway (or the model's own determinism) would replay
    the prior reply, so every repair comes back IDENTICAL and the loop stalls without progress. A
    fresh id per call makes each request body unique -- a response cache can't hit -- and nudges a
    deterministic model off its last answer. It carries no instruction and the model ignores it."""
    return f"\n\n<!-- request-id: {uuid.uuid4().hex} (unique per call; ignore) -->"


@runtime_checkable
class Llm(Protocol):
    """A model behind one call: ``complete(prompt) -> str``. Everything above depends on this,
    not on a vendor, so it stubs cleanly and swaps freely."""

    async def complete(self, prompt: str) -> str: ...


class Usage(BaseModel):
    """Token counts for one call or a run's cumulative total, split by the classes Anthropic bills
    separately: fresh ``input`` / ``output``, plus cache ``read`` (a prompt-cache hit) and ``write``
    (writing a prompt into the cache)."""

    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(
            input=self.input + other.input,
            output=self.output + other.output,
            cache_read=self.cache_read + other.cache_read,
            cache_write=self.cache_write + other.cache_write,
        )


#: Anthropic prompt-cache pricing RELATIVE to the base input price: a cache WRITE (creating the
#: cached prefix) costs ~1.25x input, a cache READ (a hit on it) ~0.10x. :meth:`Pricing.from_env`
#: fills the two cache prices from these when they are not set explicitly, so cache hits are billed.
_CACHE_WRITE_MULT = 1.25
_CACHE_READ_MULT = 0.10


class Pricing(BaseModel):
    """Per-token USD prices as **dollars per million tokens** for each usage class (so cost is the
    API's own usage counts × these). Defaults are 0.0 -- set them for the model in use; the totals
    then reflect real spend. Fully serialisable config."""

    input: float = 0.0
    output: float = 0.0
    cache_read: float = 0.0
    cache_write: float = 0.0

    @classmethod
    def from_env(cls) -> "Pricing":
        """Prices from ``WEB_PRICE_INPUT`` / ``WEB_PRICE_OUTPUT`` / ``WEB_PRICE_CACHE_READ`` /
        ``WEB_PRICE_CACHE_WRITE`` ($/million tokens). The two CACHE prices, when not set, are DERIVED
        from the input price at Anthropic's prompt-cache ratios (a cache read ~0.10x input, a cache
        write ~1.25x) -- so a cached prefix (the page skeleton re-read across authoring retries) is
        never silently billed at $0 and under-reports spend. So a spend report is configured purely
        from the env -- for ANY :class:`AnthropicLlm`, not only the CLI's."""

        def _price(name: str) -> "float | None":
            raw = os.environ.get(name)
            if not raw:
                return None  # unset -> let the caller apply its default
            try:
                return float(raw)
            except ValueError:
                return None

        inp = _price("WEB_PRICE_INPUT") or 0.0
        read = _price("WEB_PRICE_CACHE_READ")
        write = _price("WEB_PRICE_CACHE_WRITE")
        return cls(
            input=inp,
            output=_price("WEB_PRICE_OUTPUT") or 0.0,
            cache_read=read if read is not None else inp * _CACHE_READ_MULT,
            cache_write=write if write is not None else inp * _CACHE_WRITE_MULT,
        )

    def cost(self, usage: Usage) -> float:
        """The USD cost of ``usage`` at these prices."""
        return (
            usage.input * self.input
            + usage.output * self.output
            + usage.cache_read * self.cache_read
            + usage.cache_write * self.cache_write
        ) / 1_000_000


class RateLimit(BaseModel):
    """A minimum interval (seconds) between calls -- politeness for a shared/corporate key or an LLM
    PROXY. 0.0 is no limit. Every :class:`Llm` client serialises calls through a :class:`_Gate`, so
    the interval holds under concurrency and caps the request rate regardless of backend."""

    min_interval: float = 0.0

    @classmethod
    def from_env(cls) -> "RateLimit":
        """The rate from ``WEB_LLM_RATE`` (minimum seconds between LLM calls); 0 / unset -> no limit."""
        try:
            return cls(min_interval=float(os.environ.get("WEB_LLM_RATE", "0") or "0"))
        except ValueError:
            return cls()


class _Gate:
    """A shared min-interval throttle: hold ``rate.min_interval`` between calls, serialised so it
    holds under concurrency. Used by every LLM client so ``WEB_LLM_RATE`` caps the request rate to
    the LLM (proxy) whichever backend (API or ``claude -p`` shim) is driving it."""

    def __init__(self, rate: "RateLimit | None") -> None:
        self._rate = rate or RateLimit.from_env()
        self._lock = asyncio.Lock()
        self._last = 0.0

    async def hold(self) -> None:
        if self._rate.min_interval <= 0.0:
            return
        async with self._lock:
            wait = self._rate.min_interval - (time.monotonic() - self._last)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last = time.monotonic()


class LlmEvent(BaseModel):
    """One LLM call, published on the event bus so a caller can report cost AS IT GOES: this call's
    ``cost_usd`` + token :class:`Usage`, and the client's RUNNING ``spent_usd`` / ``calls``."""

    topic: str = "llm"
    model: str = ""
    calls: int = 0
    cost_usd: float = 0.0
    spent_usd: float = 0.0
    usage: Usage = Usage()


class ReasonEvent(BaseModel):
    """A WHY published on the bus -- the reasoning behind a choice, so a caller can log why things
    were chosen: ``stage`` names the phase (``frontier`` / ``author`` / ``review``) and ``text`` is
    the reason (optionally about ``subject`` -- e.g. the URL picked)."""

    topic: str = "reason"
    stage: str = ""
    subject: str = ""
    text: str = ""


def _int(obj: object, key: str) -> int:
    """A non-negative int field from a decoded-JSON object (0 when absent / not a number)."""
    if isinstance(obj, dict):
        value = obj.get(key)
        if isinstance(value, (int, float)):
            return int(value)
    return 0


class AnthropicLlm:
    """An :class:`Llm` over the Anthropic Messages API. Fully env-configurable (an explicit argument
    always wins): ``model`` <- ``WEB_LLM_MODEL``; ``auth`` <- ``WEB_LLM_API_KEY`` / ``ANTHROPIC_API_KEY``;
    ``base_url`` <- ``WEB_LLM_BASE_URL`` / ``ANTHROPIC_BASE_URL``. ``rate`` throttles calls (a shared
    key) <- ``WEB_LLM_RATE``; ``pricing`` turns the API's usage counts into a running spend
    (:attr:`spent_usd`, :attr:`usage`, :attr:`calls`) <- ``WEB_PRICE_*`` when not passed, so the
    spend report is configured from the env for ANY client, not only the CLI's. Never leaks httpx
    errors -- an API/transport failure raises a structured
    :class:`~web.fetch.WebException` (``llm.request`` / ``llm.api``)."""

    def __init__(
        self,
        *,
        model: "str | None" = None,
        auth: "str | None" = None,
        max_tokens: int = 1024,
        base_url: "str | None" = None,
        system: "str | None" = None,
        rate: "RateLimit | None" = None,
        pricing: "Pricing | None" = None,
    ) -> None:
        env = os.environ.get
        self._model = model or env("WEB_LLM_MODEL") or "claude-sonnet-5"
        self._auth = auth or env("WEB_LLM_API_KEY") or env("ANTHROPIC_API_KEY", "") or ""
        base = base_url or env("WEB_LLM_BASE_URL") or env("ANTHROPIC_BASE_URL")
        self._max_tokens = max_tokens
        self._system = system
        #: WEB_LLM_RATE (min seconds between calls) applies even to a directly-built AnthropicLlm().
        self._gate = _Gate(rate)
        #: WEB_PRICE_* ($/M tokens) apply even to a directly-built AnthropicLlm(), so a spend report
        #: is configured from the env everywhere -- not only through the CLI's --price-* flags.
        self._pricing = pricing or Pricing.from_env()
        self._client = httpx.AsyncClient(base_url=base or "https://api.anthropic.com", timeout=60.0)
        #: cumulative metering across this client's calls.
        self.usage = Usage()
        self.spent_usd = 0.0
        self.calls = 0

    def _meter(self, data: object) -> None:
        """Fold one response's ``usage`` into the running totals + spend."""
        raw = data.get("usage") if isinstance(data, dict) else None
        one = Usage(
            input=_int(raw, "input_tokens"),
            output=_int(raw, "output_tokens"),
            cache_read=_int(raw, "cache_read_input_tokens"),
            cache_write=_int(raw, "cache_creation_input_tokens"),
        )
        cost = self._pricing.cost(one)
        self.usage = self.usage + one
        self.spent_usd += cost
        self.calls += 1
        emit(
            LlmEvent(
                model=self._model,
                calls=self.calls,
                cost_usd=cost,
                spent_usd=self.spent_usd,
                usage=one,
            )
        )  # report this call live

    async def complete(self, prompt: str) -> str:
        await self._gate.hold()
        body: dict[str, object] = {
            "model": self._model,
            "max_tokens": self._max_tokens,
            # the anti-cache marker makes each request body unique so a caching gateway can't replay
            # a prior identical reply (which would stall the author loop's repairs -- see the helper).
            "messages": [{"role": "user", "content": prompt + anticache_suffix()}],
        }
        if self._system is not None:
            body["system"] = self._system
        try:
            resp = await self._client.post(
                "/v1/messages",
                headers={
                    "x-api-key": self._auth,
                    "anthropic-version": "2023-06-01",
                    "cache-control": "no-store",  # ask an intermediary proxy not to cache the reply
                },
                json=body,
            )
        except httpx.HTTPError as exc:
            raise WebException(err("llm.request", str(exc))) from exc
        if resp.status_code != 200:
            # fold the API's own error text into the MESSAGE (not only the body) so a caller that
            # logs str(exc) -- e.g. the author loop's verdict.error -- sees WHY (an invalid key, an
            # unknown model, a rate limit), not a bare "HTTP 401".
            snippet = " ".join(resp.text.split())[:200]
            detail = (
                f"HTTP {resp.status_code}: {snippet}" if snippet else f"HTTP {resp.status_code}"
            )
            raise WebException(err("llm.api", detail, body=resp.text[:500]))
        data: object = resp.json()
        self._meter(data)
        content = data.get("content", []) if isinstance(data, dict) else []
        parts = [
            block["text"]
            for block in content
            if isinstance(block, dict)
            and block.get("type") == "text"
            and isinstance(block.get("text"), str)
        ]
        return "".join(parts)

    async def aclose(self) -> None:
        await self._client.aclose()


__all__ = ["Llm", "AnthropicLlm", "Usage", "Pricing", "RateLimit", "LlmEvent", "ReasonEvent"]
