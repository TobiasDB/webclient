"""web.llm -- the LLM client: a minimal ``Llm`` protocol and a real Anthropic implementation.

The rest of the system drives a model through the ``Llm`` interface (``complete(prompt) -> str``),
so an agent's driver is testable with a stub and the concrete client is swappable. This layer
depends only on web.kernel (+ httpx); it knows nothing of pages, selectors, or agents -- those
compose it (see web.agent's llm_driver).
"""

from __future__ import annotations

from .client import AnthropicLlm, Llm

__all__ = ["Llm", "AnthropicLlm"]
