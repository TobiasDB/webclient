"""An MCP adapter -- the WebClient's task verbs as Model Context Protocol tools.

MCP is the way most agents consume a browsing/scraping capability, and this is an
*adapter*, not new capability: each tool is a thin wrapper over an existing verb
(``fetch``/``render``/``search``/``crawl``/``discover_sitemaps``) or the plan machinery
(write + validate + run a lazy expression from a blob). The tool registry
(:func:`build_tools`) is plain data + handlers, so it is testable with no MCP SDK
installed; :func:`serve` wires it onto an stdio MCP server, importing the ``mcp``
SDK lazily (with a clear install hint if it is missing).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from .query.expr import from_plan
from .surfaces import WebClient, default_client

#: a JSON-schema fragment shared by the url-taking verbs.
_URL = {"type": "string", "description": "an absolute http(s) URL"}


@dataclass(frozen=True)
class Tool:
    """One MCP tool: its ``name``, a one-line ``description``, a JSON-Schema
    ``input_schema`` for its arguments, and a ``handler`` that runs it against a
    bound client and returns a JSON-serialisable result."""

    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Callable[[dict[str, Any]], Any]


def _schema(**props: Any) -> dict[str, Any]:
    required = [k for k, v in props.items() if v.pop("_required", False)]
    return {
        "type": "object",
        "properties": props,
        "required": required,
        "additionalProperties": False,
    }


def _crawl_result(crawl: Any) -> dict[str, Any]:
    return {
        "pages": [p.model_dump() for p in crawl.pages],
        "urls": [p.transport.final_url for p in crawl.pages if p.transport],
        "frontier": [e.model_dump() for e in crawl.frontier],
        "done": crawl.done,
    }


def build_tools(client: WebClient | None = None) -> list[Tool]:
    """The tool registry, bound to ``client`` (or the process-local default). Each
    handler takes the tool's argument dict and returns a JSON-serialisable value."""

    def wc() -> WebClient:
        return client if client is not None else default_client()

    def markdown(a: dict[str, Any]) -> str:
        return str(wc().fetch(a["url"]).render("markdown"))

    def text(a: dict[str, Any]) -> str:
        return str(wc().fetch(a["url"]).render("text", main_content_only=True))

    def links(a: dict[str, Any]) -> list[str]:
        return [r.url for r in wc().fetch(a["url"]).render("links")]

    def skeleton(a: dict[str, Any]) -> str:
        return wc().fetch(a["url"], browser=a.get("browser", False)).skeleton()

    def summary(a: dict[str, Any]) -> dict[str, Any]:
        facets = a.get("facets") or []
        return wc().fetch(a["url"], browser=a.get("browser", False)).summary(*facets).model_dump()

    def search(a: dict[str, Any]) -> list[dict[str, Any]]:
        return [h.model_dump() for h in wc().search(a["query"], limit=int(a.get("limit", 10)))]

    def discover_sitemaps(a: dict[str, Any]) -> list[str]:
        return [r.url for r in wc().discover_sitemaps(a["url"])]

    def crawl(a: dict[str, Any]) -> dict[str, Any]:
        c = wc().crawl(
            a["url"], auto=True,
            max_pages=int(a.get("max_pages", 20)),
            browser=a.get("browser", True),
            keywords=a.get("keywords"),
            include=a.get("include"), exclude=a.get("exclude"),
            facets=a.get("facets"),
        ).run()
        return _crawl_result(c)

    def validate_plan(a: dict[str, Any]) -> dict[str, Any]:
        expr = from_plan(a.get("blob") or a["plan"], wc())
        return {"valid": True, "describe": expr._plan.describe(), "blob": expr.to_blob()}

    def run_plan(a: dict[str, Any]) -> Any:
        from .service import _serialize

        expr = from_plan(a.get("blob") or a["plan"], wc())
        context = wc().ref(a["url"]) if a.get("url") else None
        return _serialize(wc().execute(expr, context), {})

    def lazy_query_guide(a: dict[str, Any]) -> str:
        from .guides import lazy_query_guide as _guide

        return _guide()

    return [
        Tool("fetch_markdown", "Fetch a URL and return its content as markdown.",
             _schema(url={**_URL, "_required": True}), markdown),
        Tool("fetch_text", "Fetch a URL and return its readable text (chrome stripped).",
             _schema(url={**_URL, "_required": True}), text),
        Tool("links", "Fetch a URL and return its outbound link URLs.",
             _schema(url={**_URL, "_required": True}), links),
        Tool("skeleton", "Fetch a URL and return a token-lean DOM skeleton "
             "(an HTML-tag outline) to write CSS selectors from. Set browser='probe' "
             "for a JS/SPA page: injected nodes are marked [xhr]/[js] and data APIs listed.",
             _schema(url={**_URL, "_required": True}, browser={"type": "string"}), skeleton),
        Tool("summary", "Fetch a URL and return a token-lean structured summary. "
             "'facets' picks which sections (transport/metadata/structure/runtime/probe).",
             _schema(url={**_URL, "_required": True},
                     facets={"type": "array", "items": {"type": "string"}},
                     browser={"type": "boolean"}), summary),
        Tool("search", "Web search; returns structured hits (title/url/description).",
             _schema(query={"type": "string", "_required": True},
                     limit={"type": "integer"}), search),
        Tool("discover_sitemaps", "Discover a site's real sitemap.xml page URLs.",
             _schema(url={**_URL, "_required": True}), discover_sitemaps),
        Tool("crawl", "Bounded, same-origin crawl from a seed URL; a summary per page "
             "plus the unresolved frontier. Renders each page in a browser by default "
             "(browser=true) so JS/lazy links load -- set browser=false for a faster "
             "static crawl of a server-rendered site. Resource links (images/scripts/"
             "media) are dropped and each edge carries an importance 'score' (nav/'read "
             "more'/article high, footer/legal/social low); the frontier is sorted by "
             "it, so the useful links lead. Steer with 'keywords'/'include'/'exclude'.",
             _schema(url={**_URL, "_required": True},
                     max_pages={"type": "integer"},
                     browser={"type": "boolean"},
                     keywords={"type": "array", "items": {"type": "string"}},
                     include={"type": "string"}, exclude={"type": "string"},
                     facets={"type": "array", "items": {"type": "string"}}), crawl),
        Tool("validate_plan", "Validate a lazy-expression plan (object) or blob (string) "
             "and return its human-readable description + a compact blob -- author a "
             "plan and check it before running.",
             _schema(plan={"type": "object"}, blob={"type": "string"}), validate_plan),
        Tool("run_plan", "Run a lazy-expression plan (object) or blob (string). Optional "
             "'url' supplies the fetch context. Rebuilt + name-validated before it runs.",
             _schema(plan={"type": "object"}, blob={"type": "string"}, url=_URL), run_plan),
        Tool("lazy_query_guide", "Return the lazy-query syntax guide -- how to author a "
             "lazy extraction plan (wq roots, select/extract/filter/project, operators, "
             "blobs). Read it before writing a plan for validate_plan / run_plan.",
             _schema(), lazy_query_guide),
    ]


def dispatch(name: str, args: dict[str, Any], client: WebClient | None = None) -> Any:
    """Run one tool by name (the dispatch the MCP server performs) -- the testable
    seam. Raises ``KeyError`` for an unknown tool."""
    for tool in build_tools(client):
        if tool.name == name:
            return tool.handler(args)
    raise KeyError(name)


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
