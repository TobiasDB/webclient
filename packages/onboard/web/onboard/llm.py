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
from typing import Protocol, runtime_checkable

import httpx
from pydantic import BaseModel
from web.fetch import WebException, emit, err


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


class Pricing(BaseModel):
    """Per-token USD prices as **dollars per million tokens** for each usage class (so cost is the
    API's own usage counts × these). Defaults are 0.0 -- set them for the model in use; the totals
    then reflect real spend. Fully serialisable config."""

    input: float = 0.0
    output: float = 0.0
    cache_read: float = 0.0
    cache_write: float = 0.0

    def cost(self, usage: Usage) -> float:
        """The USD cost of ``usage`` at these prices."""
        return (
            usage.input * self.input
            + usage.output * self.output
            + usage.cache_read * self.cache_read
            + usage.cache_write * self.cache_write
        ) / 1_000_000


class RateLimit(BaseModel):
    """A minimum interval (seconds) between calls -- politeness for a shared/corporate key. 0.0 is
    no limit. The client serialises calls so the interval holds under concurrency."""

    min_interval: float = 0.0


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
    were chosen: ``stage`` names the phase (``frontier`` / ``evaluate`` / ``author`` / ``review``)
    and ``text`` is the reason (optionally about ``subject`` -- e.g. the URL picked)."""

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
    """An :class:`Llm` over the Anthropic Messages API. ``auth`` defaults to ``ANTHROPIC_API_KEY``;
    ``model`` is the API model string. ``rate`` throttles calls (a shared key); ``pricing`` turns
    the API's usage counts into a running spend (:attr:`spent_usd`, :attr:`usage`, :attr:`calls`).
    Never leaks httpx errors -- an API/transport failure raises a structured
    :class:`~web.fetch.WebException` (``llm.request`` / ``llm.api``)."""

    def __init__(
        self,
        *,
        model: str = "claude-sonnet-5",
        auth: str | None = None,
        max_tokens: int = 1024,
        base_url: str = "https://api.anthropic.com",
        system: str | None = None,
        rate: "RateLimit | None" = None,
        pricing: "Pricing | None" = None,
    ) -> None:
        self._model = model
        self._auth = auth if auth is not None else os.environ.get("ANTHROPIC_API_KEY", "")
        self._max_tokens = max_tokens
        self._system = system
        self._rate = rate or RateLimit()
        self._pricing = pricing or Pricing()
        self._client = httpx.AsyncClient(base_url=base_url, timeout=60.0)
        #: cumulative metering across this client's calls.
        self.usage = Usage()
        self.spent_usd = 0.0
        self.calls = 0
        self._lock = asyncio.Lock()
        self._last = 0.0

    async def _throttle(self) -> None:
        """Hold at least ``rate.min_interval`` between calls (serialised, so it holds concurrently)."""
        if self._rate.min_interval <= 0.0:
            return
        async with self._lock:
            wait = self._rate.min_interval - (time.monotonic() - self._last)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last = time.monotonic()

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
        await self._throttle()
        body: dict[str, object] = {
            "model": self._model,
            "max_tokens": self._max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }
        if self._system is not None:
            body["system"] = self._system
        try:
            resp = await self._client.post(
                "/v1/messages",
                headers={"x-api-key": self._auth, "anthropic-version": "2023-06-01"},
                json=body,
            )
        except httpx.HTTPError as exc:
            raise WebException(err("llm.request", str(exc))) from exc
        if resp.status_code != 200:
            raise WebException(err("llm.api", f"HTTP {resp.status_code}", body=resp.text[:500]))
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
