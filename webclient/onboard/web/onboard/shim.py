"""A REAL-model :class:`Llm` backed by the local ``claude -p`` CLI (Claude Code, subscription --
NO API key), for authoring without an Anthropic key. It drives ``claude -p`` headless with the
agent system prompt REPLACED by a minimal "be a precise text function" one, tools disabled, and the
dynamic (cwd/git/memory) sections stripped -- so it behaves like a raw text completion and returns
clean output. A one-shot ``complete`` spawns one CLI process per call (slow: ~30s with the
process start + a cold prompt); a :meth:`ClaudeShim.conversation` keeps ONE process alive over
``--input-format stream-json`` and feeds it turn after turn -- the model keeps the history, the
prompt cache hits, and a turn takes a second or two. The author / evaluate loops use it through
the :class:`~web.onboard.llm.Conversational` seam. Draws on the Claude Code plan's usage/rate
limits. Needs the ``claude`` CLI on ``PATH``.
"""

from __future__ import annotations

import asyncio
import json
import os
from asyncio.subprocess import PIPE

from web.fetch import WebException, emit, err

from .llm import (
    _BACKOFF_BASE,
    _BACKOFF_CAP,
    Budget,
    LlmEvent,
    RateLimit,
    ReasonEvent,
    Usage,
    _Gate,
    anticache_suffix,
)


def _int(value: object) -> int:
    return int(value) if isinstance(value, (int, float)) else 0


_SYSTEM = (
    "You are a precise text function. Do exactly what the user's message instructs and output ONLY "
    "the requested content -- no preamble, no explanation, no markdown code fences unless the "
    "instruction asks for them. When the message asks for JSON, reply with that JSON and nothing "
    "else. Do not use any tools; answer from the message alone."
)


async def _reap(proc: "asyncio.subprocess.Process") -> None:
    """Reap a killed child so it does not linger as a zombie -- bounded, and never raises."""
    try:
        await asyncio.wait_for(proc.wait(), timeout=5.0)
    except (asyncio.TimeoutError, ProcessLookupError, OSError):
        pass


class ClaudeShim:
    """An :class:`Llm` (``complete(prompt) -> str``) over ``claude -p``. ``model`` picks a CLI model
    alias (``"haiku"`` -- the cheapest -- by default; ``None`` uses the CLI default). Records the
    last prompt/reply. Raises :class:`~web.fetch.WebException` on failure."""

    def __init__(
        self,
        *,
        model: "str | None" = None,
        timeout: "float | None" = None,
        rate: "RateLimit | None" = None,
        budget: "Budget | None" = None,
        max_retries: "int | None" = None,
    ) -> None:
        env = os.environ.get
        #: a timed-out / crashed / non-JSON `claude -p` call is RETRIED with backoff (a transient
        #: stall must not abort a whole authoring run) -- WEB_LLM_RETRIES, default 2 for the shim.
        try:
            self._max_retries = (
                max_retries if max_retries is not None else int(env("WEB_LLM_RETRIES", "2"))
            )
        except ValueError:
            self._max_retries = 2
        self._model = model or env("WEB_LLM_MODEL") or "haiku"  # WEB_LLM_MODEL / --model / "haiku"
        try:
            self._timeout = timeout if timeout is not None else float(env("WEB_LLM_TIMEOUT", "90"))
        except ValueError:
            self._timeout = 90.0
        #: WEB_LLM_RATE throttles the claude -p spawns too (a shared plan / an LLM proxy in front).
        self._gate = _Gate(rate)
        #: the spend CAP (WEB_LLM_BUDGET), charged with claude -p's OWN reported cost -- share one
        #: Budget with an API client so a whole run has a single cap whichever backend drives it.
        self.budget = budget or Budget.from_env()
        self.prompt = ""
        self.reply = ""
        #: real metering from claude -p's own accounting (``total_cost_usd`` + ``usage``).
        self.usage = Usage()
        self.spent_usd = 0.0
        self.calls = 0
        self._conversations: "list[_ShimConversation]" = []  # live processes, closed by aclose()

    def conversation(self) -> "_ShimConversation":
        """Open a multi-turn conversation over ONE persistent ``claude -p`` process (see the module
        docstring): the opening turn is sent once; every later turn rides on the kept history."""
        conv = _ShimConversation(self)
        self._conversations.append(conv)
        return conv

    def _argv(self, *, stream: bool) -> "list[str]":
        """The CLI invocation: a raw text function (our system prompt, no tools, no dynamic
        sections); ``stream`` = the persistent stream-json form."""
        return [
            "claude",
            "-p",
            "--output-format",
            "stream-json" if stream else "json",
            *(("--input-format", "stream-json", "--verbose") if stream else ()),
            "--system-prompt",
            _SYSTEM,  # answer like a raw completion, not a coding agent
            "--exclude-dynamic-system-prompt-sections",  # drop cwd/git/memory noise
            "--allowed-tools",
            "",  # no tools: pure text in/out
            *(("--model", self._model) if self._model else ()),
        ]

    async def complete(self, prompt: str) -> str:
        self.budget.ensure()  # stop BEFORE spending past the cap (raises BudgetExceeded)
        self.prompt = prompt
        for attempt in range(self._max_retries + 1):
            await self._gate.hold()  # WEB_LLM_RATE: cap the request rate to the model / proxy
            try:
                return await self._once(prompt)
            except WebException as exc:
                if attempt >= self._max_retries or exc.error.code != "llm.shim":
                    raise
                delay = min(_BACKOFF_CAP, _BACKOFF_BASE * (2**attempt))
                emit(
                    ReasonEvent(
                        stage="llm",
                        text=f"{exc.error.message} — retrying in {delay:.0f}s "
                        f"({attempt + 1}/{self._max_retries})",
                    )
                )
                await asyncio.sleep(delay)
        raise WebException(err("llm.shim", "exhausted retries"))  # pragma: no cover

    async def _once(self, prompt: str) -> str:
        """ONE ``claude -p`` call -> the reply (metered); any failure is an ``llm.shim`` error."""
        argv = self._argv(stream=False)
        # the prompt is passed on STDIN, never argv: an author prompt can start with "---"/"-",
        # which `claude` would else parse as an option. The anti-cache marker keeps each call unique
        # so a repair never replays a prior identical reply (a stalled loop -- see the helper).
        sent = prompt + anticache_suffix()
        try:
            proc = await asyncio.create_subprocess_exec(*argv, stdin=PIPE, stdout=PIPE, stderr=PIPE)
            try:
                out, errb = await asyncio.wait_for(
                    proc.communicate(sent.encode("utf-8")), timeout=self._timeout
                )
            except asyncio.TimeoutError as exc:
                proc.kill()  # KILL the child -- wait_for only cancels the await, leaving it running
                await _reap(proc)
                raise WebException(
                    err("llm.shim", f"claude -p timed out after {self._timeout}s")
                ) from exc
        except OSError as exc:
            raise WebException(err("llm.shim", f"could not run claude -p: {exc}")) from exc
        if proc.returncode != 0:
            raise WebException(err("llm.shim", (errb or out).decode("utf-8", "replace")[:400]))
        try:
            payload: object = json.loads(out.decode("utf-8"))
        except ValueError as exc:
            raise WebException(
                err("llm.shim", f"claude -p did not return JSON: {out[:200]!r}")
            ) from exc
        if not isinstance(payload, dict):
            raise WebException(err("llm.shim", "claude -p JSON was not an object"))
        result = payload.get("result")
        self.reply = result if isinstance(result, str) else ""
        self._meter(payload)  # report cost live from claude's own accounting
        return self.reply

    def _meter(self, payload: "dict[str, object]") -> None:
        """Fold this call's cost (``total_cost_usd``) + token usage into the running totals and
        publish an :class:`~web.onboard.LlmEvent` so a caller reports cost as it goes."""
        raw = payload.get("usage")
        usage = raw if isinstance(raw, dict) else {}
        one = Usage(
            input=_int(usage.get("input_tokens")),
            output=_int(usage.get("output_tokens")),
            cache_read=_int(usage.get("cache_read_input_tokens")),
            cache_write=_int(usage.get("cache_creation_input_tokens")),
        )
        cost_raw = payload.get("total_cost_usd")
        cost = float(cost_raw) if isinstance(cost_raw, (int, float)) else 0.0
        self.usage = self.usage + one
        self.spent_usd += cost
        self.calls += 1
        self.budget.charge(cost, one)  # the (possibly shared) cap sees every call
        emit(
            LlmEvent(
                model=self._model or "claude -p",
                calls=self.calls,
                cost_usd=cost,
                spent_usd=self.spent_usd,
                usage=one,
            )
        )

    async def aclose(self) -> None:
        """End every live conversation process (a one-shot call leaves nothing behind)."""
        for conv in self._conversations:
            await conv.aclose()
        self._conversations.clear()


class _ShimConversation:
    """A :class:`~web.onboard.llm.Conversation` over ONE persistent ``claude -p`` process: user
    turns go in as stream-json lines, each reply is the ``result`` event that closes a turn
    (metered like a one-shot call). The process starts on the first ``send``. If it dies or a
    turn times out, the next ``send`` RESTARTS it and replays the exchange so far as a catch-up
    prefix -- the loop keeps its memory at the cost of one longer turn."""

    def __init__(self, shim: ClaudeShim) -> None:
        self._shim = shim
        self._proc: "asyncio.subprocess.Process | None" = None
        self._history: "list[tuple[str, str]]" = []  # (user turn, reply) so far

    async def send(self, text: str) -> str:
        self._shim.budget.ensure()
        await self._shim._gate.hold()
        catch_up = ""
        if self._proc is None or self._proc.returncode is not None:
            if self._history:  # a restart: the process lost the history -- replay it compactly
                catch_up = (
                    "Our conversation so far (you replied to each of these):\n\n"
                    + "\n\n".join(f"[user]\n{u}\n[you]\n{r}" for u, r in self._history)
                    + "\n\nContinue from here.\n\n"
                )
            await self._start()
        assert self._proc is not None and self._proc.stdin is not None
        sent = catch_up + text + anticache_suffix()
        msg = {
            "type": "user",
            "message": {"role": "user", "content": [{"type": "text", "text": sent}]},
        }
        try:
            self._proc.stdin.write((json.dumps(msg) + "\n").encode("utf-8"))
            await self._proc.stdin.drain()
            payload = await asyncio.wait_for(self._result(), timeout=self._shim._timeout)
        except asyncio.TimeoutError as exc:
            await self._kill()
            raise WebException(
                err("llm.shim", f"claude -p (conversation) timed out after {self._shim._timeout}s")
            ) from exc
        except (BrokenPipeError, ConnectionResetError, OSError) as exc:
            await self._kill()
            raise WebException(err("llm.shim", f"claude -p (conversation) died: {exc}")) from exc
        if payload is None:  # the process ended without a result for this turn
            code = self._proc.returncode
            await self._kill()
            raise WebException(
                err("llm.shim", f"claude -p (conversation) exited (code {code}) mid-turn")
            )
        result = payload.get("result")
        reply = result if isinstance(result, str) else ""
        self._shim.reply = reply
        self._shim._meter(payload)
        self._history.append((text, reply))
        return reply

    async def _start(self) -> None:
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *self._shim._argv(stream=True), stdin=PIPE, stdout=PIPE, stderr=PIPE
            )
        except OSError as exc:
            raise WebException(err("llm.shim", f"could not run claude -p: {exc}")) from exc

    async def _result(self) -> "dict[str, object] | None":
        """Read stream-json events until the ``result`` that closes the turn (``None`` at EOF)."""
        assert self._proc is not None and self._proc.stdout is not None
        while True:
            line = await self._proc.stdout.readline()
            if not line:
                return None
            try:
                event: object = json.loads(line.decode("utf-8", "replace"))
            except ValueError:
                continue  # a non-JSON line (a warning) -- not ours
            if isinstance(event, dict) and event.get("type") == "result":
                return event

    async def _kill(self) -> None:
        if self._proc is not None and self._proc.returncode is None:
            self._proc.kill()
            await _reap(self._proc)

    async def aclose(self) -> None:
        """End the process (close its stdin, then make sure it is gone)."""
        if self._proc is None:
            return
        if self._proc.stdin is not None and not self._proc.stdin.is_closing():
            self._proc.stdin.close()
        try:
            await asyncio.wait_for(self._proc.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            await self._kill()


__all__ = ["ClaudeShim"]
