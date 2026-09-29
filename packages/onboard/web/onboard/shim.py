"""A REAL-model :class:`Llm` backed by the local ``claude -p`` CLI (Claude Code, subscription --
NO API key), for authoring without an Anthropic key. It drives ``claude -p`` headless with the
agent system prompt REPLACED by a minimal "be a precise text function" one, tools disabled, and the
dynamic (cwd/git/memory) sections stripped -- so it behaves like a raw text completion and returns
clean output. One CLI turn per call (slow), and it draws on the Claude Code plan's usage/rate
limits. Needs the ``claude`` CLI on ``PATH``.
"""

from __future__ import annotations

import asyncio
import json
from asyncio.subprocess import PIPE

from web.fetch import WebException, emit, err

from .llm import LlmEvent, Usage


def _int(value: object) -> int:
    return int(value) if isinstance(value, (int, float)) else 0


_SYSTEM = (
    "You are a precise text function. Do exactly what the user's message instructs and output ONLY "
    "the requested content -- no preamble, no explanation, no markdown code fences unless the "
    "instruction asks for them. Do not use any tools; answer from the message alone."
)


class ClaudeShim:
    """An :class:`Llm` (``complete(prompt) -> str``) over ``claude -p``. ``model`` picks a CLI model
    alias (``"haiku"`` -- the cheapest -- by default; ``None`` uses the CLI default). Records the
    last prompt/reply. Raises :class:`~web.fetch.WebException` on failure."""

    def __init__(self, *, model: "str | None" = "haiku", timeout: float = 180.0) -> None:
        self._model = model
        self._timeout = timeout
        self.prompt = ""
        self.reply = ""
        #: real metering from claude -p's own accounting (``total_cost_usd`` + ``usage``).
        self.usage = Usage()
        self.spent_usd = 0.0
        self.calls = 0

    async def complete(self, prompt: str) -> str:
        self.prompt = prompt
        argv = [
            "claude",
            "-p",
            "--output-format",
            "json",
            "--system-prompt",
            _SYSTEM,  # answer like a raw completion, not a coding agent
            "--exclude-dynamic-system-prompt-sections",  # drop cwd/git/memory noise
            "--allowed-tools",
            "",  # no tools: pure text in/out
            *(("--model", self._model) if self._model else ()),
        ]
        # the prompt is passed on STDIN, never argv: an author prompt can start with "---"/"-",
        # which `claude` would else parse as an option.
        try:
            proc = await asyncio.create_subprocess_exec(*argv, stdin=PIPE, stdout=PIPE, stderr=PIPE)
            out, errb = await asyncio.wait_for(
                proc.communicate(prompt.encode("utf-8")), timeout=self._timeout
            )
        except asyncio.TimeoutError as exc:
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
        """Nothing to close -- each call is a fresh subprocess (for a uniform ``Llm`` lifecycle)."""
        return None


__all__ = ["ClaudeShim"]
