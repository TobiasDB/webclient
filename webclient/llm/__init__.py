"""LLM-facing surface: everything for driving the library with a model.

Gathered here (rather than scattered across the package root) so the model-side of the
library lives in one place: the type-safe page-scoped :mod:`.agent` loop, the MCP server
:mod:`.mcp`, the ready-to-use task-verb :mod:`.tools`, and the :mod:`.prompts` templates.
The LLM CLIENT itself is a transport, so it lives with the other clients
(:mod:`webclient.clients.llm`); import it from there (or via the client pool).
"""

from __future__ import annotations

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
from .tools import extract, fetch_markdown, fetch_text, links, page_skeleton

__all__ = [
    # agent loop
    "drive", "Observation", "Action", "AgentRun", "Policy",
    "Click", "Type", "WaitFor", "Scroll", "Goto", "Done",
    # mcp
    "Tool", "build_tools", "dispatch", "build_server", "serve",
    # task verbs
    "fetch_markdown", "fetch_text", "links", "page_skeleton", "extract",
    # prompts
    "render_prompt",
]
