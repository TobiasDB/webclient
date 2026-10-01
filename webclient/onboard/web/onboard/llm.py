"""The ``Llm`` protocol and an Anthropic Messages-API client over httpx.

The client is CONFIG-driven and self-metering: a :class:`RateLimit` (a minimum interval between
calls, so a shared/corporate key stays under its limit) and a :class:`Pricing` (per-token USD
prices for input / output / cache-read / cache-write) are passed in, and the client tracks
cumulative :class:`Usage` and spend from the API's own usage counts. Nothing here is vendor-locked
below :class:`Llm` -- swap in any ``complete(prompt) -> str``.
"""

from __future__ import annotations

import asyncio
import json
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


@runtime_checkable
class Conversation(Protocol):
    """A running multi-turn exchange: ``send(text)`` adds a user turn and returns the reply, with
    the whole history kept in context. The OPENING turn carries the big shared prefix (a page
    skeleton + the query guide) and is sent ONCE; each later turn is a short follow-up."""

    async def send(self, text: str) -> str: ...


@runtime_checkable
class Conversational(Protocol):
    """An :class:`Llm` that can also open a :class:`Conversation` -- the seam the author uses to
    send the page once and keep it in context across retries (see :class:`AnthropicLlm`). A plain
    ``complete``-only model (the ``claude -p`` shim) has no memory, so the author re-sends the
    opening each turn as its fallback."""

    async def complete(self, prompt: str) -> str: ...
    def conversation(self) -> Conversation: ...


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


class BudgetExceeded(RuntimeError):
    """Raised BEFORE an LLM call that would run while the budget is already spent out -- so a run
    stops cleanly (a defined stop reason) instead of racking up cost. Carries the running
    ``spent_usd`` and the ``limit_usd`` that was crossed for the report."""

    def __init__(self, spent_usd: float, limit_usd: float) -> None:
        self.spent_usd = spent_usd
        self.limit_usd = limit_usd
        super().__init__(f"LLM budget exceeded: spent ${spent_usd:.4f} of ${limit_usd:.4f} cap")


class Budget(BaseModel):
    """Running LLM spend in USD with an optional HARD CAP. It always ACCUMULATES (spend, calls,
    tokens) and, when ``max_usd`` is set, ENFORCES it: :meth:`ensure` -- called before every LLM
    request -- raises :class:`BudgetExceeded` once the cap is reached. ``max_usd=None`` tracks
    without capping. One Budget can be SHARED by several clients (a locate + an author model) so a
    whole onboarding run has one cap. ``WEB_LLM_BUDGET`` sets the cap from the env."""

    max_usd: "float | None" = None
    spent_usd: float = 0.0
    calls: int = 0
    usage: Usage = Usage()

    @classmethod
    def from_env(cls) -> "Budget":
        """A Budget capped at ``WEB_LLM_BUDGET`` (USD); unset / unparsable -> uncapped."""
        raw = os.environ.get("WEB_LLM_BUDGET")
        try:
            return cls(max_usd=float(raw)) if raw else cls()
        except ValueError:
            return cls()

    def ensure(self) -> None:
        """Raise :class:`BudgetExceeded` if the cap has already been reached."""
        if self.max_usd is not None and self.spent_usd >= self.max_usd:
            raise BudgetExceeded(self.spent_usd, self.max_usd)

    def charge(self, cost: float, usage: Usage) -> None:
        """Record one call's ``cost`` + ``usage`` against the running totals."""
        self.spent_usd += cost
        self.calls += 1
        self.usage = self.usage + usage

    @property
    def remaining_usd(self) -> "float | None":
        """USD left before the cap (``None`` when uncapped); never below 0."""
        return None if self.max_usd is None else max(0.0, self.max_usd - self.spent_usd)


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


#: statuses worth a retry with backoff: a rate limit, an overloaded/transient server error.
_RETRIABLE = frozenset({408, 409, 425, 429, 500, 502, 503, 504, 529})
_BACKOFF_BASE = 1.0  # seconds, doubled per attempt (capped)
_BACKOFF_CAP = 30.0


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
        budget: "Budget | None" = None,
        transport: "httpx.AsyncBaseTransport | None" = None,
        max_retries: "int | None" = None,
    ) -> None:
        env = os.environ.get
        #: retry a rate-limited (429) / transient server (5xx / 529) / dropped-connection call this
        #: many times with exponential backoff (honouring Retry-After) -- WEB_LLM_RETRIES, default 4.
        try:
            self._max_retries = (
                max_retries if max_retries is not None else int(env("WEB_LLM_RETRIES", "4") or 4)
            )
        except ValueError:
            self._max_retries = 4
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
        #: the spend CAP + cross-client accounting (WEB_LLM_BUDGET); checked before every request.
        #: Pass one Budget to several clients so a whole run shares a single cap.
        self.budget = budget or Budget.from_env()
        #: ``transport`` lets a test mount an ``httpx.MockTransport`` -- no network is touched.
        self._client = httpx.AsyncClient(
            base_url=base or "https://api.anthropic.com", timeout=60.0, transport=transport
        )
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
        self.budget.charge(cost, one)  # the (possibly shared) cap sees every call
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
        """One-shot: a single user turn -> the reply (the anti-cache marker keeps the request body
        unique so a caching gateway can't replay a prior identical reply -- see the helper)."""
        return await self._complete_messages(
            [{"role": "user", "content": prompt + anticache_suffix()}]
        )

    def conversation(self) -> "_Conversation":
        """Open a multi-turn :class:`Conversation`: the OPENING turn is prompt-cached
        (``cache_control``), so a big shared prefix -- the page skeleton + the query guide -- is
        SENT ONCE and every later turn re-reads it from cache (billed at the cache-read rate) instead
        of re-submitting it. This is how the author keeps the page in context across repair turns
        cheaply, and how an evaluate loop probes one candidate over several short turns."""
        return _Conversation(self)

    async def _complete_messages(self, messages: "list[dict[str, object]]") -> str:
        """The single Messages-API call under every completion: budget-checked (raises
        :class:`BudgetExceeded` before spending past the cap), rate-gated, metered."""
        self.budget.ensure()
        await self._gate.hold()
        body: dict[str, object] = {
            "model": self._model,
            "max_tokens": self._max_tokens,
            "messages": messages,
        }
        if self._system is not None:
            body["system"] = self._system
        headers = {
            "x-api-key": self._auth,
            "anthropic-version": "2023-06-01",
            "cache-control": "no-store",  # ask an intermediary proxy not to cache the reply
        }
        last = ""
        for attempt in range(self._max_retries + 1):
            await self._gate.hold()  # the rate limit applies to every attempt
            try:
                resp = await self._client.post("/v1/messages", headers=headers, json=body)
            except httpx.HTTPError as exc:  # a dropped / refused connection -- retry
                last = f"transport error: {exc}"
                if attempt < self._max_retries:
                    await asyncio.sleep(self._backoff(attempt))
                    continue
                raise WebException(err("llm.request", last)) from exc
            if resp.status_code == 200:
                break
            # fold the API's own error text into the MESSAGE (not only the body) so a caller that
            # logs str(exc) sees WHY (an invalid key, an unknown model, an overload), not a bare code.
            snippet = " ".join(resp.text.split())[:300]
            rid = resp.headers.get("request-id") or resp.headers.get("x-request-id") or ""
            detail = (
                f"HTTP {resp.status_code} from the Messages API (model {self._model}, attempt "
                f"{attempt + 1}/{self._max_retries + 1}"
                + (f", request-id {rid}" if rid else "")
                + ")"
                + (f": {snippet}" if snippet else " (empty error body)")
            )
            if resp.status_code in _RETRIABLE and attempt < self._max_retries:
                delay = self._retry_after(resp) or self._backoff(attempt)
                emit(
                    ReasonEvent(
                        stage="llm",
                        text=f"{detail} — retrying in {delay:.0f}s "
                        f"({attempt + 1}/{self._max_retries})",
                    )
                )
                await asyncio.sleep(delay)
                continue
            raise WebException(err("llm.api", detail, body=resp.text[:500]))  # not retriable
        else:  # pragma: no cover - every path above returns / raises / breaks
            raise WebException(err("llm.request", last or "exhausted retries"))
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

    @staticmethod
    def _backoff(attempt: int) -> float:
        """The exponential backoff delay (seconds) for a retry attempt, capped."""
        return min(_BACKOFF_CAP, _BACKOFF_BASE * (2.0**attempt))

    @staticmethod
    def _retry_after(resp: httpx.Response) -> "float | None":
        """The server's ``Retry-After`` (seconds, capped at 60), or ``None`` when absent/unparseable
        so the caller falls back to plain backoff."""
        value = resp.headers.get("retry-after")
        try:
            return min(float(value), 60.0) if value else None
        except ValueError:
            return None

    async def aclose(self) -> None:
        await self._client.aclose()


def json_blob(text: str) -> str:
    """The first balanced JSON object/array in a model reply (models like to wrap it in prose or a
    code fence). STRING-AWARE: a brace/bracket inside a JSON string value (``"the } brace"``, a
    selector like ``[class*="price"]`` in a reason) does not miscount depth. Falls back to the whole
    stripped text."""
    t = text.strip()
    if t.startswith("```"):  # drop a code fence
        t = t.split("\n", 1)[-1]
        if t.rstrip().endswith("```"):
            t = t.rstrip()[:-3]
        t = t.strip()
    starts = [i for i in (t.find("{"), t.find("[")) if i != -1]
    if not starts:
        return t
    start = min(starts)
    depth = 0
    in_str = False
    escaped = False
    for i in range(start, len(t)):
        ch = t[i]
        if in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            depth += 1
        elif ch in "}]":
            depth -= 1
            if depth == 0:
                return t[start : i + 1]
    return t[start:]


def parse_json(text: str) -> "object | None":
    """Parse the JSON value a model was asked to reply with, defensively -- ``None`` when the reply
    holds no valid JSON (the caller then retries with a short "reply with ONLY valid JSON" turn, or
    falls back). The JSON tier's one parsing seam, so every step reads replies the same way."""
    try:
        value: object = json.loads(json_blob(text))
    except ValueError:
        return None
    return value


class _Conversation:
    """A running multi-turn conversation over an :class:`AnthropicLlm` (its :class:`Conversation`).
    The opening user turn is marked ``cache_control: ephemeral`` so its (large) content is
    prompt-cached; each later :meth:`send` appends only the short follow-up and the cached opening is
    RE-READ rather than re-sent. The anti-cache marker is added per turn: the opening's marker is
    stored in the history and re-sent byte-identical every turn, so it can never break the cached
    prefix -- it only makes THIS conversation's requests distinct from another's."""

    def __init__(self, client: AnthropicLlm) -> None:
        self._client = client
        self._messages: "list[dict[str, object]]" = []

    async def send(self, text: str) -> str:
        """Add ``text`` as the next user turn, complete it, and return the reply (budget-checked and
        metered like a normal call). The first turn is cache-marked."""
        turn = text + anticache_suffix()
        if not self._messages:  # the opening: cache-mark it so follow-ups don't re-send it
            self._messages.append(
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": turn, "cache_control": {"type": "ephemeral"}}
                    ],
                }
            )
        else:
            self._messages.append({"role": "user", "content": turn})
        reply = await self._client._complete_messages(self._messages)
        self._messages.append({"role": "assistant", "content": reply})
        return reply


__all__ = [
    "Llm",
    "Conversation",
    "Conversational",
    "AnthropicLlm",
    "Usage",
    "Pricing",
    "RateLimit",
    "Budget",
    "BudgetExceeded",
    "LlmEvent",
    "ReasonEvent",
]
