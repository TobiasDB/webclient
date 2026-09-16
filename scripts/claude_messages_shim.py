"""A Messages-API shim over the local Claude Code CLI -- so the real onboarding
``LlmClient`` (and anything speaking the Anthropic Messages API) can point ``--base-url``
at it instead of a paid API key, for TESTING.

It exposes ``POST /v1/messages``: it reads the Anthropic request (``system`` + ``messages``),
runs the prompt through ``claude -p`` (headless, subscription-backed), and returns an
Anthropic-shaped message response (``content`` text block + ``usage``). Any ``x-api-key``
is accepted and ignored.

Run:
    env/bin/python scripts/claude_messages_shim.py --port 8787
    # then, in another shell:
    ANTHROPIC_API_KEY=dummy env/bin/python -m webclient.pipelines \
        onboard product-catalogue Acme --base-url http://127.0.0.1:8787 --budget 1 -v

NOTE (yours to weigh): routes model calls through your Claude Code subscription via
headless mode -- check that fits your plan's acceptable use; it draws on your plan's
usage/rate limits and is slow (one CLI turn per request).
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

# the shim lives in scripts/, so its sibling adapter is importable when run as a script
from claude_llm_adapter import _SYSTEM, claude_code_result
from webclient.pipelines.llm import LlmError


def _prompt_from(messages: list[dict[str, Any]]) -> str:
    """Flatten Anthropic ``messages`` into one prompt string. The pipeline sends a single
    user message whose ``content`` is the whole prompt; we also handle a content-block
    list and multiple turns for generality."""
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


def _system_from(body: dict[str, Any]) -> str:
    """The request's ``system`` (string or block list), else the minimal text-function one."""
    system = body.get("system")
    if isinstance(system, list):
        system = "".join(
            str(b.get("text", "")) for b in system
            if isinstance(b, dict) and b.get("type") == "text"
        )
    return str(system) if system else _SYSTEM


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_a: Any) -> None:  # quiet; we print our own line
        pass

    def _send(self, code: int, payload: dict[str, Any]) -> None:
        blob = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(blob)))
        self.end_headers()
        self.wfile.write(blob)

    def do_POST(self) -> None:  # noqa: N802
        if self.path.rstrip("/") != "/v1/messages":
            self._send(404, {"type": "error", "error": {"type": "not_found", "message": self.path}})
            return
        length = int(self.headers.get("content-length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError as exc:
            self._send(400, {"type": "error", "error": {"type": "invalid_request_error", "message": str(exc)}})
            return
        prompt = _prompt_from(body.get("messages") or [])
        model = str(body.get("model") or "claude-code")
        print(f"  [shim] /v1/messages  ~{len(prompt)//4} tok in  ({model})", file=sys.stderr)
        try:
            env = claude_code_result(prompt, system=_system_from(body))
        except LlmError as exc:  # surface as an Anthropic error so LlmClient reports it
            code = exc.status_code if 400 <= (exc.status_code or 0) < 600 else 502
            self._send(code, {"type": "error", "error": {"type": "api_error", "message": exc.message}})
            return
        usage = env.get("usage") or {}
        self._send(200, {
            "id": f"msg_{uuid.uuid4().hex[:24]}",
            "type": "message",
            "role": "assistant",
            "model": model,
            "content": [{"type": "text", "text": str(env.get("result", ""))}],
            "stop_reason": "end_turn",
            "usage": {
                "input_tokens": int(usage.get("input_tokens", 0) or 0),
                "output_tokens": int(usage.get("output_tokens", 0) or 0),
                "cache_creation_input_tokens": int(usage.get("cache_creation_input_tokens", 0) or 0),
                "cache_read_input_tokens": int(usage.get("cache_read_input_tokens", 0) or 0),
            },
        })


def main() -> None:
    ap = argparse.ArgumentParser(description="Messages-API shim over `claude -p`")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8787)
    args = ap.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"claude Messages-API shim on http://{args.host}:{args.port}/v1/messages "
          f"(point --base-url here; any api key works)", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
