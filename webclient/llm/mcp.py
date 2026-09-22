"""An MCP adapter -- the tool registry as Model Context Protocol tools.

MCP is the way most agents consume a browsing/scraping capability, and this is an
*adapter*, not new capability: every tool comes from :mod:`webclient.tools` (the single
registry the HTTP service and the Python verbs are generated from too), so the three
transports cannot drift. :func:`build_tools` is plain data + handlers, testable with no
MCP SDK installed; :func:`serve` wires it onto an stdio MCP server, importing the
``mcp`` SDK lazily (with a clear install hint if it is missing).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from ..interface import WebClient
from ..tools import TOOLS
from ..tools import dispatch as _dispatch


@dataclass(frozen=True)
class Tool:
    """One MCP tool: its ``name``, a one-line ``description``, a JSON-Schema
    ``input_schema`` for its arguments, and a ``handler`` that runs it against a
    bound client and returns a JSON-serialisable result."""

    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Callable[[dict[str, Any]], Any]


def build_tools(client: WebClient | None = None) -> list[Tool]:
    """The MCP tool list, bound to ``client`` (or the process-local default) -- one entry
    per registered :class:`webclient.tools.Tool`, its input schema from the pydantic model."""
    def bind(name: str) -> Callable[[dict[str, Any]], Any]:
        return lambda args: _dispatch(name, args, client)

    return [
        Tool(t.name, t.description, t.schema(), bind(t.name))
        for t in TOOLS.values()
    ]


def dispatch(name: str, args: dict[str, Any], client: WebClient | None = None) -> Any:
    """Run one tool by name (the dispatch the MCP server performs) -- the testable
    seam. Raises ``KeyError`` for an unknown tool."""
    return _dispatch(name, args, client)


def build_server(client: WebClient | None = None, *, name: str = "webclient") -> Any:
    """Wire :func:`build_tools` onto an MCP :class:`~mcp.server.Server`. Imports the
    ``mcp`` SDK lazily; raises a clear ``ImportError`` (with an install hint) if it
    is not installed."""
    try:
        import mcp.types as types  # type: ignore[import-not-found]
        from mcp.server import Server  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover - depends on an optional dep
        raise ImportError(
            "the MCP server needs the 'mcp' package -- install it with "
            "`pip install mcp` (the tool registry itself, build_tools/dispatch, "
            "works without it)."
        ) from exc

    server: Any = Server(name)
    tools = {t.name: t for t in build_tools(client)}

    @server.list_tools()  # type: ignore[untyped-decorator]
    async def _list() -> Any:
        return [
            types.Tool(name=t.name, description=t.description, inputSchema=t.input_schema)
            for t in tools.values()
        ]

    @server.call_tool()  # type: ignore[untyped-decorator]
    async def _call(name: str, arguments: dict[str, Any]) -> Any:
        import json

        result = tools[name].handler(arguments or {})
        return [types.TextContent(type="text", text=json.dumps(result, default=str))]

    return server


def serve(client: WebClient | None = None) -> None:  # pragma: no cover - I/O loop
    """Run the MCP server over stdio (blocking). Needs the ``mcp`` SDK."""
    import anyio
    from mcp.server.stdio import stdio_server  # type: ignore[import-not-found]

    server = build_server(client)

    async def _main() -> None:
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())

    anyio.run(_main)


__all__ = ["Tool", "build_tools", "dispatch", "build_server", "serve"]
