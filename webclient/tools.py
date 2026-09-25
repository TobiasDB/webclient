"""The TOOL registry -- the single source of the package's high-level functionality
(roadmap N2 / N10, decision D3). Every tool is declared ONCE here with a typed pydantic
INPUT model, a documented OUTPUT, and a handler over a bound ``WebClient``; the three
transports are GENERATED from it and cannot drift:

* Python  -- :func:`dispatch` (and the thin verbs in :mod:`webclient.llm.tools`);
* MCP     -- :func:`webclient.llm.mcp.build_tools` maps each tool to an MCP tool;
* HTTP    -- :mod:`webclient.service` mounts ``POST /tools/{name}`` (+ ``GET /tools`` for
  the schemas, and the legacy verb paths as aliases) from the same list.

``docs/tools.md`` is rendered from the registry by ``scripts/gen_docs.py tools``. Tools
are LLM-friendly but not LLM-only: they return ready-to-use values (markdown, rows,
cards, flags, hints), never plan machinery -- the ``/execute`` plan endpoint stays the
low-level tier underneath. See ``docs/user-stories.md`` for who each tool serves.

    @tool("fetch_markdown", "Fetch a URL and return its content as markdown.", story="analyst")
    def fetch_markdown(args: UrlArgs, wc: WebClient) -> str:
        return wc.fetch(args.url).render("markdown")
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Literal

from pydantic import BaseModel, Field, ValidationError

if TYPE_CHECKING:
    from .interface import WebClient

__all__ = ["views", "VIEWS", 
    "Tool", "tool", "TOOLS", "get", "dispatch", "schema", "UrlArgs", "TextArgs", "SkeletonArgs",
    "ExtractArgs", "CrawlArgs", "PlanArgs", "NoArgs", "FlagsArgs", "PatternsArgs", "ToolError",
]


class ToolError(ValueError):
    """A tool call that could not be made: an unknown tool, or arguments that do not fit
    its input model (``errors`` carries pydantic's structured list)."""

    def __init__(self, message: str, *, tool: str = "", errors: "list[Any] | None" = None) -> None:
        super().__init__(message)
        self.tool = tool
        self.errors = errors or []


# --------------------------------------------------------------------------- #
# the input models (one per argument shape; shared where the shape is the same)
# --------------------------------------------------------------------------- #

BrowserArg = "bool | Literal['never', 'auto', 'always']"


class NoArgs(BaseModel):
    """No arguments."""

    model_config = {"extra": "ignore"}


class UrlArgs(BaseModel):
    """A single absolute http(s) URL."""

    model_config = {"extra": "ignore"}

    url: str = Field(description="an absolute http(s) URL")


class TextArgs(UrlArgs):
    main_content_only: bool = Field(True, description="strip nav / header / footer chrome")


class SkeletonArgs(UrlArgs):
    browser: Any = Field(False, description="transport tier: false | 'auto' | 'always'; 'auto' renders a JS/SPA page and marks injected nodes [xhr]/[js]")
    collapse: bool = Field(False, description="fold structurally identical siblings to one line ×N")
    drop_chrome: bool = Field(False, description="omit nav / footer / sidebar landmarks")
    max_lines: int = Field(400, ge=1, le=20000, description="line budget of the outline")


class FlagsArgs(UrlArgs):
    browser: Any = Field(False, description="transport tier: false | 'auto' | 'always'")


class PatternsArgs(UrlArgs):
    for_: "Literal['extract', 'interact', 'crawl'] | None" = Field(None, alias="for", description="only the hints meant for one consumer")
    model_config = {"extra": "ignore", "populate_by_name": True}


class ExtractArgs(UrlArgs):
    result: str = Field(description="the CSS selector matching each row element")
    fields: dict[str, str] = Field(description="output column -> the CSS selector whose text is that column's value")
    limit: int | None = Field(None, ge=1, description="at most this many rows")


class CrawlArgs(UrlArgs):
    max_pages: int = Field(20, ge=1, le=10000)
    width: int = Field(10, ge=1, description="frontier edges expanded per round (best-first)")
    depth: int = Field(3, ge=0, description="max link distance from the seed")
    max_frontier: int = Field(10000, ge=1)
    same_origin: bool = True
    obey_robots: bool = True
    browser: Any = Field(False, description="transport tier: false (static, fast) | 'auto' | true/'always' (render each page)")
    keywords: list[str] | None = Field(None, description="best-first relevance hints")
    include: str | None = Field(None, description="only follow links whose path contains this")
    exclude: str | None = Field(None, description="skip links whose path contains this")
    resolve: dict[str, Any] | None = Field(None, description="a serialised Resolve policy bundle")
    session: str | None = Field(None, description="(HTTP service) run on this server-side session's identity")


class PlanArgs(BaseModel):
    """A lazy-expression plan as an object, or its blob string; ``url`` supplies the fetch
    context for a run."""

    model_config = {"extra": "ignore"}

    plan: dict[str, Any] | None = None
    blob: str | None = None
    url: str | None = Field(None, description="the fetch context for a context-rooted plan")


# --------------------------------------------------------------------------- #
# the registry
# --------------------------------------------------------------------------- #

Handler = Callable[[Any, "WebClient"], Any]


@dataclass(frozen=True)
class Tool:
    """One tool: its ``name``, a one-line ``description``, the pydantic ``input`` model, a
    ``returns`` note (what the output is), the ``handler(args, wc)``, and the user ``story``
    it serves (see docs/user-stories.md)."""

    name: str
    description: str
    input: type[BaseModel]
    handler: Handler
    returns: str = ""
    story: str = ""
    aliases: tuple[str, ...] = field(default_factory=tuple)  # legacy HTTP paths (``/markdown``)

    def schema(self) -> dict[str, Any]:
        """The JSON Schema of the input (what MCP / OpenAPI publish)."""
        s = self.input.model_json_schema(by_alias=True)
        s.pop("title", None)
        s.setdefault("type", "object")
        s.setdefault("properties", {})
        s["additionalProperties"] = False
        return s

    def parse(self, args: "dict[str, Any] | BaseModel") -> BaseModel:
        """Validate raw arguments into the input model (a :class:`ToolError` on a mismatch)."""
        if isinstance(args, self.input):
            return args
        try:
            return self.input.model_validate(dict(args) if not isinstance(args, dict) else args)
        except ValidationError as exc:
            raise ToolError(
                f"bad arguments for tool {self.name!r}: {exc.errors()[0].get('msg', 'invalid')}",
                tool=self.name, errors=exc.errors(),
            ) from exc


TOOLS: dict[str, Tool] = {}


def tool(
    name: str, description: str, *, returns: str = "", story: str = "", aliases: "tuple[str, ...]" = (),
) -> Callable[[Handler], Handler]:
    """Register ``fn(args, wc)`` as the tool ``name``; the input model is ``fn``'s first
    parameter annotation."""
    def wrap(fn: Handler) -> Handler:
        import inspect
        import typing

        first = next(iter(inspect.signature(fn).parameters))
        # only the first parameter matters; ``WebClient`` is a TYPE_CHECKING-only name here
        hints = typing.get_type_hints(fn, globalns={**fn.__globals__, "WebClient": Any})
        model = hints.get(first)
        if not (isinstance(model, type) and issubclass(model, BaseModel)):
            raise TypeError(f"tool {name!r}: the first parameter must be annotated with a pydantic model")
        TOOLS[name] = Tool(name=name, description=description, input=model, handler=fn,
                           returns=returns, story=story, aliases=tuple(aliases))
        return fn
    return wrap


def get(name: str) -> Tool:
    """The tool named ``name`` (``KeyError`` for an unknown one)."""
    return TOOLS[name]


def schema() -> "list[dict[str, Any]]":
    """Every tool's public description: name, description, input schema, returns, story."""
    return [
        {"name": t.name, "description": t.description, "input_schema": t.schema(),
         "returns": t.returns, "story": t.story, "aliases": list(t.aliases)}
        for t in TOOLS.values()
    ]


def dispatch(name: str, args: "dict[str, Any] | BaseModel", client: "WebClient | None" = None) -> Any:
    """Run one tool by name against ``client`` (or the process-local default): validate
    the arguments, call the handler, return its JSON-friendly value. ``KeyError`` for an
    unknown tool; :class:`ToolError` for bad arguments."""
    from .interface import default_client

    t = TOOLS[name]
    wc = client if client is not None else default_client()
    return t.handler(t.parse(args), wc)


# --------------------------------------------------------------------------- #
# the tools
# --------------------------------------------------------------------------- #


def _jsonable(value: Any) -> Any:
    dump = getattr(value, "model_dump", None)
    if dump is not None:
        return dump(mode="json")
    if isinstance(value, list):
        return [_jsonable(v) for v in value]
    return value


@tool("fetch_markdown", "Fetch a URL and return its content as markdown.",
      returns="markdown text", story="analyst", aliases=("markdown",))
def fetch_markdown(args: UrlArgs, wc: "WebClient") -> str:
    return str(wc.fetch(args.url).render("markdown"))


@tool("fetch_text", "Fetch a URL and return its readable text (chrome stripped).",
      returns="plain text", story="analyst", aliases=("text",))
def fetch_text(args: TextArgs, wc: "WebClient") -> str:
    return str(wc.fetch(args.url).render("text", main_content_only=args.main_content_only))


@tool("links", "Fetch a URL and return its outbound link URLs (absolute).",
      returns="a list of URL strings", story="analyst", aliases=("links",))
def links(args: UrlArgs, wc: "WebClient") -> "list[str]":
    return [r.url for r in wc.fetch(args.url).render("links")]


@tool("skeleton", "Fetch a URL and return a token-lean DOM skeleton (an HTML-tag outline) to "
      "write CSS selectors from. Set browser='auto' for a JS/SPA page: injected nodes are "
      "marked [xhr]/[js] and data APIs listed.",
      returns="the skeleton text", story="agent-developer", aliases=("skeleton",))
def skeleton(args: SkeletonArgs, wc: "WebClient") -> str:
    return str(wc.fetch(args.url, browser=args.browser).skeleton(
        collapse=args.collapse, drop_chrome=args.drop_chrome, max_lines=args.max_lines))


@tool("extract", "Extract rows from a URL: 'result' is the CSS selector per row element, "
      "'fields' maps output columns to a CSS selector whose text is the value.",
      returns="a list of row dicts", story="data-engineer")
def extract(args: ExtractArgs, wc: "WebClient") -> "list[dict[str, Any]]":
    from .interface import doc

    exprs = {name: doc.select(sel).attr("text") for name, sel in args.fields.items()}
    rows = wc.fetch(args.url).select_all(args.result)
    if args.limit is not None:
        rows = rows.limit(args.limit)
    return list(rows.extract(**exprs).project())


@tool("flags", "Fetch a URL and return the flags detected on it (spa / anti-bot / login / "
      "pagination / forms / ...), each with its confidence, evidence and remedy.",
      returns="a list of Flag objects", story="agent-developer")
def flags(args: FlagsArgs, wc: "WebClient") -> "list[dict[str, Any]]":
    return _jsonable(wc.fetch(args.url, browser=args.browser).flags())  # type: ignore[no-any-return]


@tool("card", "Fetch a URL and return its lean PageCard (url / kind / title / description / "
      "flags / transport tier) -- the cheapest useful view of a page.",
      returns="a PageCard object", story="analyst")
def card(args: UrlArgs, wc: "WebClient") -> "dict[str, Any]":
    return _jsonable(wc.fetch(args.url).card())  # type: ignore[no-any-return]


@tool("patterns", "Fetch a URL and return its recurring-structure hints: the repeating record "
      "list to select_all (extract), repeated controls (interact), the page template (crawl).",
      returns="a list of PatternHint objects, most confident first", story="data-engineer")
def patterns(args: PatternsArgs, wc: "WebClient") -> "list[dict[str, Any]]":
    return _jsonable(wc.fetch(args.url).patterns(for_=args.for_))  # type: ignore[no-any-return]


class ElementsArgs(UrlArgs):
    kind: "Literal['interactive', 'content', 'records']" = Field("interactive", description="interactive = the controls; content = text-bearing leaves (repeats marked); records = the repeated-record regions to select_all")
    browser: Any = Field(False, description="transport tier: false | 'auto' | 'always'")
    limit: int = Field(200, ge=1, le=2000)


@tool("elements", "The page's numbered, class-free element table -- what a model (and the Playground) "
      "picks from by index: interactive controls, content leaves, or the repeated-record regions.",
      returns="a list of IndexedElement objects", story="agent-developer")
def elements(args: ElementsArgs, wc: "WebClient") -> "list[dict[str, Any]]":
    from .dom.index import record_options
    from .core.document.html import tree

    doc = wc.fetch(args.url, browser=args.browser)
    if args.kind == "records":
        return _jsonable(record_options(tree(doc), top_k=min(args.limit, 20)))  # type: ignore[no-any-return]
    if args.kind == "content":
        return _jsonable(doc.content_elements()[: args.limit])  # type: ignore[no-any-return]
    return _jsonable(doc.controls()[: args.limit])  # type: ignore[no-any-return]


class FieldsArgs(UrlArgs):
    record: str = Field(description="the record selector (a select_all target, e.g. from elements(kind='records'))")
    browser: Any = Field(False, description="transport tier: false | 'auto' | 'always'")
    limit: int = Field(40, ge=1, le=400)


@tool("fields", "The extractable field leaves inside the FIRST instance of a record selector, numbered, "
      "each with a per-row selector -- what a query is built from by pointing.",
      returns="a list of IndexedElement objects (selector scoped to the record)", story="data-engineer")
def fields(args: FieldsArgs, wc: "WebClient") -> "list[dict[str, Any]]":
    from .dom.index import field_options
    from .core.document.html import tree

    doc = wc.fetch(args.url, browser=args.browser)
    return _jsonable(field_options(tree(doc), args.record, limit=args.limit))  # type: ignore[no-any-return]


class SnapshotArgs(UrlArgs):
    browser: Any = Field(False, description="transport tier: false | 'auto' | 'always'")
    include: list[str] = Field(default_factory=list, description=(
        "extra views in the same response: 'rrweb' (the page as rrweb Meta + FullSnapshot, for the "
        "player), 'patterns' (the pattern hints), 'records' (the repeating-region options), 'flags'"))


@tool("snapshot", "Fetch a URL and return its captured content (HTML/JSON/text) with the card -- the page the "
      "Playground renders in its preview -- plus, on request, the rrweb snapshot, the pattern hints, "
      "the record options and the flags, in ONE round trip.",
      returns="{card, content, kind, encoding, rrweb?, patterns?, records?, flags?}", story="agent-developer")
def snapshot(args: SnapshotArgs, wc: "WebClient") -> "dict[str, Any]":
    return views(wc.fetch(args.url, browser=args.browser), ["card", "content", *args.include])


#: every view a document can be asked for -- what a UI needs to show a page
VIEWS = ("card", "content", "rrweb", "patterns", "records", "flags", "skeleton", "markdown", "controls",
         "elements", "transport")


def views(doc: Any, include: "list[str]") -> "dict[str, Any]":
    """The requested views of an already-fetched document, in one dict: ``card``,
    ``content``, ``rrweb`` (Meta + FullSnapshot for the player), ``patterns``, ``records``
    (the repeating-region options), ``flags``, ``skeleton``, ``markdown``, ``controls``,
    ``elements`` (the content elements), ``transport``. Always carries ``document_id``,
    ``kind``, ``url``, ``title`` and ``live``."""
    out: dict[str, Any] = {"document_id": doc.name, "kind": doc.kind, "encoding": doc.encoding,
                           "url": doc.final_url or doc.url, "title": doc.title, "live": getattr(doc, "_page", None) is not None,
                           "tiers": list(getattr(doc, "_tiers", []) or [])}
    want = set(include)
    if "card" in want:
        out["card"] = _jsonable(doc.card())
    if "content" in want:
        out["content"] = (doc.content or b"").decode(doc.encoding or "utf-8", "replace")
    if "rrweb" in want and doc.kind == "html":
        from .models import SnapshotEvent
        from .replay.rrweb import to_rrweb

        snap = SnapshotEvent(url=doc.url, final_url=doc.final_url or doc.url, kind="html", content=doc.content,
                             status_code=doc.status_code, document_id=doc.name, ts=0.0)
        out["rrweb"] = to_rrweb([snap], custom=False)
    if "patterns" in want:
        out["patterns"] = _jsonable(doc.patterns())
    if "records" in want:
        from .dom.index import record_options
        from .core.document.html import tree

        out["records"] = _jsonable(record_options(tree(doc), top_k=20)) if doc.kind == "html" else []
    if "flags" in want:
        out["flags"] = _jsonable(doc.flags())
    if "skeleton" in want:
        out["skeleton"] = doc.skeleton(collapse=True) if doc.kind == "html" else ""
    if "markdown" in want:
        out["markdown"] = doc.markdown() if doc.kind == "html" else out.get("content", "")
    if "controls" in want:
        out["controls"] = _jsonable(doc.controls()) if doc.kind == "html" else []
    if "elements" in want:
        out["elements"] = _jsonable(doc.content_elements()) if doc.kind == "html" else []
    if "transport" in want:
        out["transport"] = _jsonable(doc.transport())
    return out


@tool("sitemap", "Hunt a site's sitemap.xml page URLs (cheap -- not a crawl).",
      returns="a list of URL strings", story="data-engineer", aliases=("sitemap",))
def sitemap(args: UrlArgs, wc: "WebClient") -> "list[str]":
    return [r.url for r in wc.sitemap(args.url)]


@tool("robots", "Hunt a site's robots.txt: its Sitemap: URLs and raw rules.",
      returns="a Robots object", story="data-engineer", aliases=("robots",))
def robots(args: UrlArgs, wc: "WebClient") -> "dict[str, Any]":
    return _jsonable(wc.robots(args.url))  # type: ignore[no-any-return]


@tool("crawl", "Bounded, same-origin crawl from a seed URL; a lean record per page "
      "(url/status/kind/title) plus the unresolved frontier. Static by default (fast); set "
      "browser='auto' or true to render JS pages so lazy links load. Resource links are "
      "dropped and each edge carries an importance 'score' (nav/'read more'/article high, "
      "footer/legal/social low); the frontier is sorted by it. Steer with 'keywords' / "
      "'include' / 'exclude'.",
      returns="{pages, urls, frontier, frontier_total, done}", story="data-engineer", aliases=("crawl",))
def crawl(args: CrawlArgs, wc: "WebClient") -> "dict[str, Any]":
    from .policy import Resolve

    c = wc.crawl(
        args.url, auto=True, width=args.width, depth=args.depth, max_pages=args.max_pages,
        max_frontier=args.max_frontier, same_origin=args.same_origin, obey_robots=args.obey_robots,
        browser=args.browser, resolve=Resolve.model_validate(args.resolve) if args.resolve else None,
        keywords=args.keywords, include=args.include, exclude=args.exclude,
    ).run()
    return {
        "pages": [
            {"url": p.final_url or p.url, "status": p.status_code, "kind": p.kind,
             "title": p.title if getattr(p, "has_op", lambda _n: True)("title") else None}
            for p in c.pages
        ],
        "urls": [p.final_url or p.url for p in c.pages],
        "frontier": [e.model_dump() for e in c.frontier[: c.config.width]],
        "frontier_total": len(c.frontier),
        "done": c.done,
    }


@tool("validate_plan", "Validate a lazy-expression plan (object) or blob (string) and return its "
      "human-readable description + a compact blob -- author a plan and check it before running.",
      returns="{valid, describe, blob}", story="agent-developer")
def validate_plan(args: PlanArgs, wc: "WebClient") -> "dict[str, Any]":
    from .query.expr import from_plan

    expr = from_plan(args.blob or args.plan or {}, wc)
    return {"valid": True, "describe": expr._plan.describe(), "blob": expr.to_blob()}


@tool("run_plan", "Run a lazy-expression plan (object) or blob (string). Optional 'url' supplies "
      "the fetch context. Rebuilt + name-validated before it runs.",
      returns="the plan's result (rows / a value / a document handle)", story="agent-developer")
def run_plan(args: PlanArgs, wc: "WebClient") -> Any:
    from .query.expr import from_plan
    from .service import _serialize

    expr = from_plan(args.blob or args.plan or {}, wc)
    context = wc.ref(args.url) if args.url else None
    return _serialize(wc.execute(expr, context), {})


@tool("lazy_query_guide", "Return the lazy-query syntax guide -- how to author a lazy extraction "
      "plan (wq roots, select/extract/filter/project, operators, blobs). Read it before writing "
      "a plan for validate_plan / run_plan.",
      returns="markdown", story="agent-developer")
def lazy_query_guide(args: NoArgs, wc: "WebClient") -> str:
    from .llm.guides import lazy_query_guide as _guide

    return _guide()
