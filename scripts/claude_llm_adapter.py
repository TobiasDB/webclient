"""Mock the onboarding LLM with the local Claude Code CLI (subscription-backed) instead
of a paid Anthropic API key -- for TESTING the pipeline end to end.

The onboarding ``llm`` is just ``Callable[[str], str]``, so we shell out to ``claude -p``
(headless print mode) with the Claude Code system prompt REPLACED by a minimal "be a
precise text function" one, tools disabled, and the dynamic (cwd/git/memory) sections
stripped -- so it behaves like a raw text completion and returns clean output.

Usage:
    uv pip install --python env/bin/python ddgs      # real web search (no key)
    env/bin/python scripts/claude_llm_adapter.py ir-news Adobe --budget-note

NOTE (yours to weigh): this routes the pipeline's model calls through your Claude Code
subscription via headless mode -- check that fits your plan's acceptable use. It also
draws on your plan's usage/rate limits and is slow (one CLI turn per call).
"""

from __future__ import annotations

import json
import subprocess
import sys

from webclient import WebClient
from webclient.pipelines.llm import LlmError
from webclient.pipelines.onboarding import Brief, ddg_search, onboard

# a minimal system prompt so Claude Code answers like a raw completion, not a coding agent
_SYSTEM = (
    "You are a precise text function. Do exactly what the user's message instructs and "
    "output ONLY the requested content -- no preamble, no explanation, no markdown code "
    "fences unless the instruction asks for them. Do not use any tools; answer from the "
    "message alone."
)


def claude_code_result(prompt: str, *, system: str = _SYSTEM, timeout: float = 180.0) -> dict:
    """Run one prompt through ``claude -p`` and return its full JSON envelope (``result``
    text + ``usage``). Raises :class:`LlmError` on failure."""
    try:
        out = subprocess.run(
            [
                "claude", "-p", prompt,
                "--output-format", "json",
                "--system-prompt", system,             # replace the agent system prompt
                "--exclude-dynamic-system-prompt-sections",  # drop cwd/git/memory noise
                "--allowed-tools", "",                 # no tools: pure text in/out
            ],
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise LlmError(0, f"claude -p timed out after {timeout}s") from exc
    if out.returncode != 0:
        raise LlmError(out.returncode, (out.stderr or out.stdout or "claude -p failed")[:400])
    try:
        payload = json.loads(out.stdout)
    except json.JSONDecodeError as exc:
        raise LlmError(0, f"claude -p did not return JSON: {out.stdout[:200]}") from exc
    if payload.get("is_error"):
        raise LlmError(0, str(payload.get("result") or payload.get("subtype") or "error")[:400])
    return payload


def claude_code_llm(prompt: str, *, timeout: float = 180.0) -> str:
    """Run one onboarding prompt through ``claude -p`` and return its text result."""
    return str(claude_code_result(prompt, timeout=timeout).get("result", ""))


def main() -> None:
    if len(sys.argv) < 3:
        sys.exit("usage: claude_llm_adapter.py <brief> <company> [<company> ...]")
    brief_name, companies = sys.argv[1], sys.argv[2:]
    brief = Brief.load(brief_name)  # a packaged brief name or a markdown path
    calls = {"n": 0}

    def llm(prompt: str) -> str:
        calls["n"] += 1
        print(f"  [llm call #{calls['n']}] ~{len(prompt)//4} tok in", file=sys.stderr)
        return claude_code_llm(prompt)

    with WebClient() as wc:
        results = onboard(companies, brief, wc=wc, llm=llm, search=ddg_search, review=True)
    ok = sum(1 for r in results if r.ok)
    print(f"\ndone: {ok}/{len(results)} onboarded via Claude Code ({calls['n']} llm calls)")


if __name__ == "__main__":
    main()
