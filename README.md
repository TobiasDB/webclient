# webclient

A declarative web client: fetch pages, select and extract structured data,
render to markdown, drive a real browser, and run the **same plan** synchronously,
asynchronously, or against a remote "browser-as-a-service" backend.

The whole library is one idea: the surface you call **is** a typed `Core` model,
and calling it dispatches an op that runs immediately -- `wc.fetch(url)` hands
back a `Document`, no ceremony. Sync / async / remote are just different
*dispatchers* on the same cores; `.lazy` on any surface records a **plan** you
batch or defer instead. Behaviour lives in small `Backing` classes attached to
the cores, and the typed surface is **generated** from those backings
(`scripts/gen_stubs.py`), so the types never drift from the runtime.

> Status: a solid, well-typed engine kernel with a task-verb layer
> (`webclient.tools`), truly incremental streaming, a stateful **crawl** +
> **sitemap.xml** discovery, token-lean **summary** facets, a resiliency policy
> layer (adaptive/probe browser modes + proxy/rate/retry headers), serialisable
> lazy-expression **blobs**, and an **MCP** adapter. Runnable case studies live in
> [`examples/`](examples/).

## Install

```bash
pip install -e ".[local]"      # local parsing (lxml/cssselect) -- the common case
pip install -e ".[browser]"    # + Playwright, for live browser pages
pip install -e ".[service]"    # + FastAPI/uvicorn, for the HTTP service
```

## Quickstart

```python
from webclient import WebClient

with WebClient() as wc:
    page = wc.fetch("https://example.com")   # eager -> a Document
    print(page.ok, page.title)
    print(page.markdown())                              # page as markdown
    for link in page.links():                           # a Collection[Reference]
        print(link.url)
```

`wc.fetch(url)` resolves immediately and returns a `Document` (sugar for
`wc.ref(url).resolve()`). To batch or defer, use `wc.lazy` -- it records a plan
run by `.collect()`: `wc.lazy.fetch(url).select(".t").text_content.collect()`.

The response kind (`html` / `json` / `xml` / `binary`) is sniffed from the
`Content-Type` then the leading bytes, and each kind gets its own ops (dotted-path
`select` on json, tree `select` on html/xml). If a server mislabels or omits its
type, pass an explicit hint: `wc.fetch(url, expect="json")` (`None` by default, so
a wrong guess is never forced).

### Select and extract

```python
from webclient import WebClient, doc

with WebClient() as wc:
    page = wc.fetch("https://shop.example/")

    # eager: walk a materialised Document
    for card in page.select_all(".card"):
        title = card.select(".title").text_content            # a str
        href = card.select("a").attr("href")          # a Reference (link attrs narrow)
        print(title, href.url)

    # a Collection fans out per element, then flattens to rows
    rows = (
        wc.fetch("https://shop.example/")
        .select_all(".card")
        .extract(
            title=doc.select(".title").text_content,
            link=doc.select("a").attr("href"),
        )
        .project()      # -> list[dict]
    )
```

`select` takes CSS or XPath; a selected node is itself a `Document`, so selection
nests. `attr("href"/"src"/"action")` returns a `Reference` you can `.resolve()`;
other attributes return a `Field`. Rendering has typed named methods —
`.markdown()`, `.text(main_content_only=True)`, `.links()`, `.elements()` (typed
blocks), `.html()`, `.skeleton()` — over the generic `render(format)` dispatch.

`page.skeleton()` is a **token-lean DOM outline** (`tag#id.class`, bloat removed,
repeated siblings collapsed) — feed it to an LLM to write CSS selectors for the
page cheaply, then use them with `select`/`extract`. See
[docs/llm-lazy-queries.md](docs/llm-lazy-queries.md) for a guide to building lazy
extraction queries with concrete examples.

## Task verbs -- for scripts and LLM tools

`webclient.tools` wraps the surface in a few functions that hide the plan
machinery and return ready-to-use values (markdown / text / links / rows) -- the
shape a quick script or an LLM tool wants:

```python
from webclient.tools import fetch_markdown, fetch_text, links, page_skeleton, extract

md = fetch_markdown("https://example.com")               # -> str (markdown)
text = fetch_text("https://example.com")                 # -> str (nav stripped)
urls = links("https://example.com")                      # -> list[str]
skel = page_skeleton("https://example.com")              # -> str (selector map)
rows = extract(                                          # -> list[dict]
    "https://shop.example/",
    ".card",                                             # a CSS selector per row
    {"title": ".title", "price": ".price"},              # column -> CSS selector
    limit=20,
)
```

`wc.search(query)` is robust: it sends a browser `User-Agent` (engines block a
library one) and falls back across providers, with `browser=True` for the strongest
anti-bot bypass. The same verbs are exposed as MCP tools and HTTP endpoints (incl.
`POST /skeleton`), and `summary(url, "skeleton")` puts the selector map on the
summary's `.skeleton` field.

Each accepts an optional `client=` (defaults to a process-local one; pass your own
`with WebClient() as wc` for lifecycle control).

## Async -- the same plans, awaited

```python
import asyncio
from webclient import AsyncWebClient, doc

async def main():
    async with AsyncWebClient() as ac:
        page = await ac.fetch("https://example.com")   # await at the IO boundary
        rows = await (
            ac.lazy.fetch("https://shop.example/")
            .select_all(".card")
            .extract(title=doc.select(".title").text_content)
            .project()
            .acollect()
        )
    return page.title, rows

asyncio.run(main())
```

The async client is the same eager surface over an async dispatcher: `await
ac.fetch(url)` resolves and returns a `Document`; in-memory ops on it are
synchronous. Chain deeper IO through `ac.lazy` plans, realized with
`.acollect()` / `.astream()` (the async twins of `.collect()` / `.stream()`). A
*reusable* plan built from the `doc`/`ref` module roots is run against a supplied
context: `plan.acollect(ac.ref(url))` (sync: `plan.collect(wc.ref(url))`).

## Sessions

A session is a scoped identity (cookies/headers/ttl) sharing the client's engine.
It is a context manager, so it always closes.

```python
with WebClient() as wc, wc.session(ttl=300, headers={"x-app": "demo"}) as s:
    s.fetch("https://site/login")      # sets cookies, kept on the session
    me = s.fetch("https://site/whoami")
```

## Live browser pages

With the `browser` extra, resolve on a real page and interact with it:

```python
live = wc.ref("https://app.example/").resolve(browser=True)
live.click("#load-more")
print(live.select("#cart li").text_content)
wc.release(live)   # return the page to the pool
```

## Summary -- a page's token-lean view (for LLMs)

`summary()` projects a page into a small, uniform structure -- transport facts,
head/schema metadata, body shape -- keys and counts, not raw HTML. It is what an
LLM reads *instead of* the page:

```python
s = wc.summary("https://example.com/")
s.metadata.title        # "Example Domain"  (metadata is None on a non-html page)
s.structure.word_count  # 19
s.structure.toc         # [TocEntry(level=1, text=...), ...]

wc.summary(url, "transport", "metadata")   # pick facets; a crawl carries a lean default
```

Facets: `transport`, `metadata`, `structure`, `runtime` (browser-only signals),
`probe` (what resolution needed -- see below). Naming an arbitrary backing op (e.g.
`summary(url, "title")`) adds it under `summary().extra`.

## Crawl & sitemap

A crawl is a stateful, client-held context manager -- a steerable frontier you
drive turn by turn, or let auto-drive best-first by keyword:

```python
with wc.crawl("https://books.example/", auto=True, max_pages=20,
              keywords=["pricing"]) as crawl:
    crawl.run()                     # or crawl.step(select=...) to steer each round
for page in crawl.pages:            # each a lean .summary()
    if page.transport and page.metadata:            # facets are None when N/A
        print(page.transport.final_url, page.metadata.title)
for edge in crawl.frontier:         # discovered-but-unfetched, best links first
    print(edge.score, edge.url)     # nav/"read more"/article high; footer/legal low
```

The frontier is cleaned and ranked for you: links to page **resources**
(images/scripts/media) are dropped, and each edge carries an importance `score`
(page region + anchor text + URL shape) that the frontier is **sorted by**, so the
useful links (nav, "read more", article permalinks) lead and footer/legal/social
links sink. Set `browser=True` to render JS-heavy pages first (each load waits for
the DOM to settle, so client-rendered links are captured); a browser crawl also
adds the page's XHR/data-API endpoints to the frontier.

Each page carries a lean default summary (`transport` + `metadata`); pass
`facets=[...]` to widen or narrow it. `wc.discover_sitemaps(url)` discovers a site's real
`sitemap.xml` URLs (robots `Sitemap:` directives, the well-known path, one level of
`<sitemapindex>`); `wc.sitemap(url)` maps a site, seeding from that discovery.

## Resiliency -- browser tiers & policies

`browser=` picks the transport tier, escalation is opt-in:

- `False` (default) -- static only.
- `"auto"` -- static, escalate to a browser only if the page looks JS-gated
  (empty / SPA shell). Conservative, to avoid paying for a browser needlessly.
- `True` / `"always"` -- straight to a browser.
- `"probe"` -- **explicit diagnostic**: resolve *both* tiers and compare, returning
  the fuller document with an accurate `probe` facet (`was_browser_required`,
  `render_gain` = how many visible words the browser recovered). The "can I scrape
  this / what do I need" mode -- use it to build a content-complete summary.

```python
d = wc.fetch(url, browser="probe")
p = d.summary().probe        # was_browser_required=True, render_gain=242 -> JS-gated
```

A `Resolve` policy bundle (`retry` / `rate` / `proxy`) can be set on the client; its
rate/retry/proxy concerns are declared to a downstream proxy service as
`X-WebClient-*` request headers (`WebClient(resolve=Resolve(proxy=ProxyPolicy(...)))`).

## Lazy plans as portable blobs

A recorded plan serialises to a short, url-safe **blob** an agent can store, log or
send over the wire, then rebuild + validate + pretty-print before running:

```python
from webclient import from_blob, wq

plan = wq.ref.resolve().select_all(".quote").extract(
    text=wq.doc.select(".text").text_content).project()
blob = plan.to_blob()                       # "p1:..." (a few dozen chars)
rows = from_blob(blob, wc).collect(wc.ref(url))   # rebuilt + name-validated, then run
```

## MCP & task-verb endpoints

`webclient.mcp` exposes the verbs (fetch/markdown/links/summary/search/crawl/
discover_sitemaps) plus plan authoring as Model Context Protocol tools -- the way agents
consume this category. The registry (`build_tools` / `dispatch`) works with no MCP
SDK installed; `serve()` runs an stdio server. The HTTP service mirrors them as
task-verb endpoints (`POST /markdown`, `/summary`, `/crawl`, `/plan`, ...).

## Dispatch modes -- sync, async, remote

The surface **is** the core; how an op actually runs is the core's *dispatch
mode* -- one concept, three modes, same interface:

- **sync** (`WebClient`) -- IO blocks on a background engine loop.
- **async** (`AsyncWebClient`) -- loop-native: IO runs on your loop, `await`ed.
  Only the sync client uses the engine loop.
- **remote** (`RemoteWebClient`) -- every op that needs the server becomes a
  one-request POST to a `webclient.service` app.

`.lazy` on any surface records a **plan** instead of running op-by-op, so a whole
chain or fan-out realises in a single pass (`.collect()` / `await .acollect()`).

## Remote -- browser-as-a-service

The remote client is *literally* a `WebClient` in remote mode: the same eager
surface, executed server-side over HTTP (no local browser or lxml needed). A
fetched document comes back as a real `Document` handle (metadata inline); its
content ops run **eagerly, one round trip each**, and a reference comes back as a
real `Reference`.

```python
from webclient import RemoteWebClient

with RemoteWebClient("http://host:8000", token="secret") as rc:
    handle = rc.fetch("https://example.com")        # one round trip -> a handle
    markdown = handle.render("markdown")            # each op round-trips eagerly
    # batch a chain (or a fan-out) into ONE round trip via .lazy:
    titles = handle.lazy.select_all(".title").text_content.collect()
```

**Chattiness**: because remote content ops are eager, a long op-by-op chain is a
round trip per op. Reach for `.lazy` (above) to batch a chain/fan-out into one
request -- the client logs a one-time nudge toward it once a chain gets greedy.
Sessions are the same surface, scoped: `with rc.session() as s: s.fetch(url)`
resolves through a server-side session (its cookies/identity).

Serve it with `webclient.service.create_app(token=..., max_docs=..., max_sessions=...)`.

## Errors and safety

```python
from webclient import RETURN

d = wc.fetch("https://might-fail/")                      # raises on non-2xx (loud by default)
d = wc.fetch("https://might-fail/", optional=True)       # or lenient: a not-ok Document
if not d.ok:
    print(d.error.type, d.error.status_code, d.error.retriable)  # retriable: transport/429/5xx
```

- `error=RETURN` / `optional=True` turn a failure into a not-ok `Document` instead
  of raising; `extract`/`filter` run their sub-expressions leniently so one bad
  field never aborts a whole plan.
- `WebClient(block_private_hosts=True)` is an opt-in SSRF guard: requests to
  loopback / private / link-local hosts (resolved, so a public name pointing
  inward is caught too) are refused before any transport. Off by default.

## Architecture (one screen)

- `core/web_core.py` -- `WebCore` + `Backing`: a core CHOOSES which backings
  apply to its state and DISPATCHES an op to the first that provides it. The
  eager surface IS the core (`WebCore.__getattr__` dispatches); sync/async/remote
  are just dispatchers on it.
- `core/<kind>/` -- one package per core (`reference` / `document` / `client` /
  `session` / `crawl` / `remote`), each with the core (a pydantic model) and one
  backing per module.
- `query/` -- the recorder engine: `expr.py` (the `Expr` recorder), `plan.py`
  (the serialisable `Plan` IR + `to_blob`/`from_blob` -- the wire form for the
  service/remote), and `executor.py` (one async walk over a `Plan`).
- `resiliency/` -- pure response classification (`detect.py`, so a local and a
  remote resolve agree on what to escalate) + the `X-WebClient-*` policy headers.
- `surfaces/eager.py` / `surfaces/lazy.py` / `collection.py` -- the typed eager
  and lazy surfaces (generated by `scripts/gen_stubs.py` from the backing
  signatures); `surfaces/lazy.py` also holds the `wq` authoring roots.
- `service.py` + `mcp.py` + `clients/` -- the HTTP service (task verbs + `/execute`
  + `/plan`), the MCP adapter, and the transport pool (http/browser leases).
- `examples/` -- runnable live case studies (news scraper, catalogue crawler,
  lazy-expression extractor, sitemap mapper, browser events, error handling).

## Development

```bash
env/bin/python -m pytest -q                              # tests
env/bin/python scripts/gen_stubs.py                      # regenerate typed stubs
env/bin/python scripts/gen_stubs.py --check              # fail if stubs are stale
env/bin/mypy --strict webclient && env/bin/pyright webclient   # full strictness
env/bin/black webclient scripts tests demo.py            # format
env/bin/python demo.py                                   # end-to-end showcase
```

If you change the typed surface (a Core field or a backing op signature),
regenerate the stubs and re-run `--check`; the whole package is kept
`mypy --strict` and `pyright` clean by tests in `tests/test_typing.py`.
