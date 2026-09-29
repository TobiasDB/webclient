"""A REAL-model :class:`~web.onboard.Llm` for the eval, backed by the local ``claude -p`` CLI
(Claude Code, subscription -- no API key). This is what the eval authors with: an actual model,
not a hand-rolled stand-in, so PASS/FAIL reflects real query-writing quality.

``claude -p`` is driven headless with the agent system prompt REPLACED by a minimal "be a precise
text function" one, tools disabled, and the dynamic (cwd/git/memory) sections stripped -- so it
behaves like a raw text completion and returns clean output. One CLI turn per call (slow), and it
draws on the Claude Code plan's usage/rate limits.
"""

from __future__ import annotations

import asyncio
import json
from asyncio.subprocess import PIPE

from web.fetch import WebException, err

_SYSTEM = (
    "You are a precise text function. Do exactly what the user's message instructs and output ONLY "
    "the requested content -- no preamble, no explanation, no markdown code fences unless the "
    "instruction asks for them. Do not use any tools; answer from the message alone."
)


class ClaudeShim:
    """An :class:`~web.onboard.Llm` (``complete(prompt) -> str``) over ``claude -p``. ``model`` picks
    a CLI model alias (``"haiku"`` -- the cheapest -- by default; ``None`` uses the CLI default).
    Records the last prompt/reply for the report. Raises :class:`~web.fetch.WebException` on failure.
    """

    def __init__(self, *, model: "str | None" = "haiku", timeout: float = 180.0) -> None:
        self._model = model
        self._timeout = timeout
        self.prompt = ""
        self.reply = ""

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
        result = payload.get("result") if isinstance(payload, dict) else None
        self.reply = result if isinstance(result, str) else ""
        return self.reply


__all__ = ["ClaudeShim"]
