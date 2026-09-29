"""The ``Llm`` protocol and an Anthropic Messages-API client over httpx."""

from __future__ import annotations

import os
from typing import Protocol, runtime_checkable

import httpx

from web.kernel import WebException, err


@runtime_checkable
class Llm(Protocol):
    """A model behind one call: ``complete(prompt) -> str``. Everything above depends on this,
    not on a vendor, so it stubs cleanly and swaps freely."""

    async def complete(self, prompt: str) -> str: ...


class AnthropicLlm:
    """An :class:`Llm` over the Anthropic Messages API. ``auth`` defaults to ``ANTHROPIC_API_KEY``;
    ``model`` is configurable (the API model string). Never leaks httpx errors -- an API/transport
    failure raises a structured :class:`~web.kernel.WebException` (``llm.request`` / ``llm.api``)."""

    def __init__(
        self, *, model: str = "claude-sonnet-5", auth: str | None = None,
        max_tokens: int = 1024, base_url: str = "https://api.anthropic.com",
        system: str | None = None,
    ) -> None:
        self._model = model
        self._auth = auth if auth is not None else os.environ.get("ANTHROPIC_API_KEY", "")
        self._max_tokens = max_tokens
        self._system = system
        self._client = httpx.AsyncClient(base_url=base_url, timeout=60.0)

    async def complete(self, prompt: str) -> str:
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
        content = data.get("content", []) if isinstance(data, dict) else []
        parts = [
            block["text"] for block in content
            if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str)
        ]
        return "".join(parts)

    async def aclose(self) -> None:
        await self._client.aclose()


__all__ = ["Llm", "AnthropicLlm"]
