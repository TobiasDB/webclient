# webclient

A declarative web client: fetch pages, select and extract structured data,
render to markdown, drive a real browser, and run the **same plan** synchronously,
asynchronously, or against a remote "browser-as-a-service" backend.

The whole library is one idea: you author a **lazy plan** by chaining ordinary-
looking calls; a single async executor runs it; the sync / async / remote / lazy
"flavours" are the same plan executed differently. Behaviour lives in small
`Backing` classes attached to typed `Core` models, and the typed surface you call
is **generated** from those backings (`scripts/gen_stubs.py`), so the types never
drift from the runtime.

> Status: a solid, well-typed engine kernel with a task-verb layer
> (`webclient.tools`). Crawling and true incremental streaming are not built yet.

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
    page = wc.fetch("https://example.com").collect()   # a Document
    print(page.ok, page.title)
    print(page.render("markdown"))                      # page as markdown
    for link in page.render("links"):                   # a Collection[Reference]
        print(link.url)
```

`wc.fetch(url)` records a lazy plan (statically a `LazyDocument`); `.collect()`
runs it and hands back the materialised `Document`. `wc.fetch(...)` is just sugar
for `wc.ref(url).resolve()`.

### Select and extract

```python
from webclient import WebClient, doc

with WebClient() as wc:
    page = wc.fetch("https://shop.example/").collect()

    # eager: walk a materialised Document
    for card in page.select_all(".card"):
        title = card.select(".title").text            # a str
        href = card.select("a").attr("href")          # a Reference (link attrs narrow)
        print(title, href.url)

    # lazy: one plan that fans out per element, then flattens to rows
    rows = (
        wc.fetch("https://shop.example/")
        .select_all(".card")
        .extract(
            title=doc.select(".title").attr("text"),
            link=doc.select("a").attr("href"),
        )
        .collect()      # -> a Collection
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
        page = await ac.execute(ac.fetch("https://example.com"))
        rows = await ac.execute(
            ac.fetch("https://shop.example/")
            .select_all(".card")
            .extract(title=doc.select(".title").attr("text"))
        )
    return page.title, rows

asyncio.run(main())
```

## Sessions

A session is a scoped identity (cookies/headers/ttl) sharing the client's engine.
It is a context manager, so it always closes.

```python
with WebClient() as wc, wc.session(ttl=300, headers={"x-app": "demo"}) as s:
    s.fetch("https://site/login").collect()      # sets cookies, kept on the session
    me = s.fetch("https://site/whoami").collect()
```

## Live browser pages

With the `browser` extra, resolve on a real page and interact with it:

```python
live = wc.ref("https://app.example/").resolve(browser=True).collect()
live.click("#load-more")
print(live.select("#cart li").text)
wc.release(live)   # return the page to the pool
```

## Remote -- browser-as-a-service

The remote client is *literally* a `WebClient` over a swapped core: the same
surface, executed server-side over HTTP (no local browser or lxml needed).

```python
from webclient import RemoteWebClient

with RemoteWebClient("http://host:8000", token="secret") as rc:
    handle = rc.fetch("https://example.com").collect()   # a lightweight handle
    markdown = rc.execute(handle.render("markdown"))     # one round trip per op
```

Serve it with `webclient.service.create_app(token=..., max_docs=..., max_sessions=...)`.

## Errors and safety

```python
from webclient import RETURN

d = wc.fetch("https://might-fail/").collect()            # raises on non-2xx (loud by default)
d = wc.fetch("https://might-fail/", optional=True).collect()  # or lenient: a not-ok Document
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
  apply to its state and DISPATCHES an op to the first that provides it.
- `core/{reference,document,client,session}_core.py`, `core/live.py` -- the cores
  (pydantic data models) and their backings.
- `expr.py` + `plan.py` -- the lazy recorder and its serialisable `Plan` IR (the
  wire form for the service/remote).
- `executor.py` -- one async walk over a `Plan`.
- `surface.py` / `surfaces.py` / `collection.py` / `models.py` -- the typed eager
  and lazy surfaces, generated by `scripts/gen_stubs.py` from the backing
  signatures.
- `service.py` + `core/remote_core.py` + `pool.py` + `engine/` -- the HTTP
  service, remote core, transport pool and engine loop.

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
