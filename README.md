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
> (`webclient.tools`) and truly incremental streaming. Crawling is not built yet.

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
    print(page.render("markdown"))                      # page as markdown
    for link in page.render("links"):                   # a Collection[Reference]
        print(link.url)
```

`wc.fetch(url)` resolves immediately and returns a `Document` (sugar for
`wc.ref(url).resolve()`). To batch or defer, use `wc.lazy` -- it records a plan
run by `.collect()`: `wc.lazy.fetch(url).select(".t").text_content.collect()`.

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
other attributes return a `Field`. `render(...)` supports
`"markdown"`, `"text"` (`main_content_only=True` to strip nav/chrome),
`"elements"` (typed blocks), `"links"`, and `"html"`.

## Task verbs -- for scripts and LLM tools

`webclient.tools` wraps the surface in a few functions that hide the plan
machinery and return ready-to-use values (markdown / text / links / rows) -- the
shape a quick script or an LLM tool wants:

```python
from webclient.tools import fetch_markdown, fetch_text, links, extract

md = fetch_markdown("https://example.com")               # -> str (markdown)
text = fetch_text("https://example.com")                 # -> str (nav stripped)
urls = links("https://example.com")                      # -> list[str]
rows = extract(                                          # -> list[dict]
    "https://shop.example/",
    ".card",                                             # a CSS selector per row
    {"title": ".title", "price": ".price"},              # column -> CSS selector
    limit=20,
)
```

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

## Remote -- browser-as-a-service

The remote client is *literally* a `WebClient` over a swapped core: the same
eager surface, executed server-side over HTTP (no local browser or lxml needed).

```python
from webclient import RemoteWebClient

with RemoteWebClient("http://host:8000", token="secret") as rc:
    handle = rc.fetch("https://example.com")        # one round trip -> a handle
    markdown = handle.render("markdown")            # each op round-trips eagerly
    # batch a chain (or a fan-out) into ONE round trip via .lazy:
    titles = handle.lazy.select_all(".title").text_content.collect()
```

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
  `session` / `remote`), each with the core (a pydantic model) and one backing
  per module.
- `query/` -- the recorder engine: `expr.py` (the `Expr` recorder), `plan.py`
  (the serialisable `Plan` IR -- the wire form for the service/remote), and
  `executor.py` (one async walk over a `Plan`).
- `surfaces/eager.py` / `surfaces/lazy.py` / `collection.py` -- the typed eager
  and lazy surfaces (generated by `scripts/gen_stubs.py` from the backing
  signatures); `surfaces/lazy.py` also holds the `wq` authoring roots.
- `service.py` + `pool.py` + `engine/` -- the HTTP service, transport pool and
  engine loop.

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
