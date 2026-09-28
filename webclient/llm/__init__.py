"""LLM-facing surface: everything for driving the library with a model.

Gathered here (rather than scattered across the package root) so the model-side of the
library lives in one place: the LLM CLIENT (:mod:`.client` -- ``LlmClient`` and its budget /
pricing types), the type-safe page-scoped :mod:`.agent` loop, the MCP server :mod:`.mcp`,
the ready-to-use task-verb :mod:`.tools`, and the :mod:`.prompts` templates. The client is a
transport by mechanism (a leasable ``Client``) but an onboard-tier concern by domain, so it
lives with the model-facing code, not in the fetch layer.
"""

from __future__ import annotations

from .client import (
    PRICING,
    Budget,
    BudgetExceeded,
    LlmClient,
    LlmError,
    LlmFactory,
    ModelPrice,
    Usage,
    cheapest_model,
    price_for,
)
from .agent import (
    Action,
    AgentRun,
    Click,
    Done,
    Goto,
    Observation,
    Policy,
    Scroll,
    Type,
    WaitFor,
    drive,
)
from .mcp import Tool, build_server, build_tools, dispatch, serve
from .prompts import render_prompt
from .query_agent import (
    QueryDecision,
    QueryObservation,
    QueryPolicy,
    QueryRun,
    build_query,
)
from .tools import extract, fetch_markdown, fetch_text, links, page_skeleton

__all__ = [
    # the LLM client (moved here from the fetch layer -- onboard-tier by domain)
    "LlmClient", "LlmFactory", "Budget", "BudgetExceeded", "LlmError",
    "Usage", "ModelPrice", "PRICING", "price_for", "cheapest_model",
    # interaction loop
    "drive", "Observation", "Action", "AgentRun", "Policy",
    "Click", "Type", "WaitFor", "Scroll", "Goto", "Done",
    # query loop
    "build_query", "QueryObservation", "QueryDecision", "QueryRun", "QueryPolicy",
    # mcp
    "Tool", "build_tools", "dispatch", "build_server", "serve",
    # task verbs
    "fetch_markdown", "fetch_text", "links", "page_skeleton", "extract",
    # prompts
    "render_prompt",
]
