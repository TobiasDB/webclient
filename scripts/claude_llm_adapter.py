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
from typing import TYPE_CHECKING, Any

from webclient import WebClient
from webclient.pipelines.llm import LlmError
from webclient.pipelines.onboarding import Brief, ddg_search, onboard

if TYPE_CHECKING:
    import httpx

    from webclient.pipelines.llm import Budget, LlmClient

# a minimal system prompt so Claude Code answers like a raw completion, not a coding agent
_SYSTEM = (
    "You are a precise text function. Do exactly what the user's message instructs and "
    "output ONLY the requested content -- no preamble, no explanation, no markdown code "
    "fences unless the instruction asks for them. Do not use any tools; answer from the "
    "message alone."
)

#: the cheapest Claude Code CLI model alias -- what ``claude -p`` should actually run so a
#: test/eval pass is as inexpensive as possible (paired with the cheapest priced model id
#: on the client side for budget accounting).
CHEAPEST_CLI_MODEL = "haiku"


def claude_code_result(
    prompt: str, *, system: str = _SYSTEM, timeout: float = 180.0, model: str | None = None
) -> dict:
    """Run one prompt through ``claude -p`` and return its full JSON envelope (``result``
    text + ``usage``). ``model`` picks a specific CLI model (e.g. ``"haiku"`` -- the
    cheapest); ``None`` uses the CLI default. Raises :class:`LlmError` on failure."""
    try:
        # pass the prompt on STDIN, not as an argv value: an onboarding prompt can START
        # with "---" (the query guide's YAML frontmatter) or "-", which `claude` would else
        # parse as a CLI option ("unknown option '---...'").
        out = subprocess.run(
            [
                "claude", "-p",
                "--output-format", "json",
                "--system-prompt", system,             # replace the agent system prompt
                "--exclude-dynamic-system-prompt-sections",  # drop cwd/git/memory noise
                "--allowed-tools", "",                 # no tools: pure text in/out
                *(("--model", model) if model else ()),  # cheapest model when asked
            ],
            input=prompt, capture_output=True, text=True, timeout=timeout,
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


def claude_code_llm(prompt: str, *, timeout: float = 180.0, model: str | None = None) -> str:
    """Run one onboarding prompt through ``claude -p`` and return its text result."""
    return str(claude_code_result(prompt, timeout=timeout, model=model).get("result", ""))


# --------------------------------------------------------------------------- #
# In-process Messages-API shim: the same thing claude_messages_shim.py exposes over HTTP,
# but as an httpx transport, so the real LlmClient (budget / pricing / retries) can speak
# the Messages API to `claude -p` with NO server or port to manage.
# --------------------------------------------------------------------------- #


def _flatten_messages(messages: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for m in messages:
        content = m.get("content", "")
        if isinstance(content, list):
            content = "".join(
                str(b.get("text", "")) for b in content
                if isinstance(b, dict) and b.get("type") == "text"
            )
        if content:
            parts.append(str(content))
    return "\n\n".join(parts)


def _flatten_system(system: Any) -> str:
    if isinstance(system, list):
        system = "".join(
            str(b.get("text", "")) for b in system
            if isinstance(b, dict) and b.get("type") == "text"
        )
    return str(system) if system else _SYSTEM


def shim_transport(
    *, cli_model: str | None = CHEAPEST_CLI_MODEL, timeout: float = 180.0
) -> "httpx.MockTransport":
    """An in-process Messages-API transport backed by ``claude -p`` -- a drop-in for
    :class:`LlmClient`'s ``transport`` so it speaks the Messages API to your Claude Code
    subscription with no HTTP server. ``cli_model`` is what ``claude -p`` actually runs
    (default: the cheapest, ``haiku``). The CLI envelope's real token ``usage`` is passed
    back, so budget / pricing accounting is honest against the client's model id."""
    import httpx

    def handle(request: "httpx.Request") -> "httpx.Response":
        body = json.loads(request.content or b"{}")
        prompt = _flatten_messages(body.get("messages", []))
        try:
            payload = claude_code_result(
                prompt, system=_flatten_system(body.get("system")),
                timeout=timeout, model=cli_model,
            )
        except LlmError as exc:  # surface as an Anthropic-shaped error for LlmClient
            return httpx.Response(
                exc.status_code or 500,
                json={"type": "error", "error": {"type": "api_error", "message": exc.message}},
            )
        text = str(payload.get("result", ""))
        usage = payload.get("usage") or {"input_tokens": len(prompt) // 4,
                                         "output_tokens": len(text) // 4}
        return httpx.Response(200, json={
            "id": "msg_shim", "type": "message", "role": "assistant",
            "model": str(body.get("model") or "claude-code"),
            "content": [{"type": "text", "text": text}],
            "usage": usage,
        })

    return httpx.MockTransport(handle)


def claude_shim_client(
    *, model: str | None = None, cli_model: str | None = CHEAPEST_CLI_MODEL,
    budget: "Budget | None" = None, timeout: float = 180.0,
) -> "LlmClient":
    """A ready :class:`LlmClient` that routes the Messages API through ``claude -p`` in
    process (see :func:`shim_transport`). ``model`` is the priced model id used for budget
    accounting (default: the cheapest); ``cli_model`` is what the CLI actually runs
    (default: the cheapest, ``haiku``). ``auth`` is a dummy the transport ignores."""
    from webclient.pipelines.llm import Budget, LlmClient, cheapest_model

    return LlmClient(
        model=model or cheapest_model(),
        auth="shim",  # ignored by the in-process transport; keeps LlmClient.auth truthy
        budget=budget or Budget(),
        transport=shim_transport(cli_model=cli_model, timeout=timeout),
    )


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
