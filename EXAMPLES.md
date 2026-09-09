# Interface by example

Worked use cases for the redesigned core, written *before* implementation so
the interface can be judged on how it reads. Each section ends with
**Friction** — where the design is still uncomfortable and a decision is owed.

Naming decisions assumed throughout (all revisable, they are what these
examples are testing):

| Name | Meaning |
|------|---------|
| `wc.resolve(ref_or_url, **opts) -> Document` | the one way a Document comes into existence |
| `ref.resolve()` | sugar for the above (no second verb — `fetch` is gone) |
| `doc` / `el` / `ref` | module-level lazy roots (callable to bind them) |
| `field("name")` | reference a column produced earlier in a pipeline |
| `Plan` | the serializable IR a lazy chain compiles to |

---

## 1. Core API — the thirty-second scrape

```python
from webclient import WebClient

with WebClient() as wc:
    doc = wc.resolve("https://example.com/blog")

    doc.status_code          # 200
    doc.final_url            # after redirects — link resolution uses THIS
    doc.attr("title")        # "Example Blog" — pseudo-attribute

    print(doc.render("markdown"))    # one render op, format by name
    for link in doc.render("links"):
        print(link.url)      # Reference objects, resolved against final_url
```

Selection returns addressed Elements:

```python
    card = doc.select(".post", index=0)
    card.attr("text")               # "Hello world — 3 min read"
    card.select("h2").attr("text")  # "Hello world"
    card.attr("href")               # -> Reference (link attrs narrow to Reference)
    card.attr("data-id")            # -> str
    card.attr("data-id", optional=True)   # -> str | None
```

**One accessor, not three.** There is no `.text` property, no `.html`
property, and no naming collision with a `text` *rendering*: `attr(name)` is a
single op covering real attributes and pseudo-attributes alike.

```python
    el.attr("text")     # normalised text content     -> str
    el.attr("html")     # outer HTML                  -> str
    el.attr("href")     # link attrs narrow           -> Reference
    el.attr("data-id")  # any real attribute          -> str
    el.attr("nope")     # LookupError, or optional=True -> None
```

Documents accept the same op against their root, so `doc.attr("title")` and
`doc.attr("text")` need no separate spellings. `doc.content` stays the raw
bytes and the render surface keeps its own verb, `render(format)` (`markdown`, `readable`, `links`,
`elements`), which now cannot be confused with an accessor.

One op means one IR node, one generated stub signature, and one place where
overloads narrow the return type — instead of `text`, `html` and `attr` each
carrying their own. The cost is that the single most common call in a scraper
is `el.attr("text")` rather than `el.text`; it pays for itself in a `map`
block, where every field then reads the same way (§6).

---

## 2. Core API — a browser-backed session flow

The capability difference is in the *backing*, not the type:

```python
with WebClient(headless=True) as wc:
    session = wc.session(ttl=300)

    doc = wc.resolve("https://app.example.com/login", browser=True,
                     session=session)

    (doc.write("#email", "me@example.com")
        .write("#password", secret)
        .click("button[type=submit]")
        .wait_stable())              # DOM-quiet, not a fixed sleep

    dash = doc.navigate("/dashboard")     # new Document, same page lease
    rows = dash.select_all("tr[data-account]")
    print([r.select("td.balance").attr("text") for r in rows])

    wc.release(dash)                 # page returns to the pool
```

Calling a browser op on an http-backed Document is a typed error, not an
`AttributeError`:

```python
    static = wc.resolve("https://example.com")
    static.click(".next")
    # UnsupportedOperation: click requires backing capability 'browser';
    #   this Document has an http backing.
    #   Re-resolve with wc.resolve(ref, browser=True).

    static.supports("interact")      # False
```

**A Document outlives its page.** `navigate()` returns a new Document and the
page lease moves to it — but the old Document stays *readable* from the
snapshot taken when it was resolved. Only ops needing the live page raise:

```python
    dash = doc.navigate("/dashboard")

    doc.render("markdown")    # fine — rendered from the snapshot
    doc.attr("text")          # fine
    doc.telemetry.requests    # fine — scoped to the login page, still
    doc.status_code           # fine

    doc.click(".next")        # StaleDocument: this Document's page moved to
                              #   /dashboard at 12:04:31. Live ops need the
                              #   Document navigate() returned.
    doc.wait_stable()         # StaleDocument
```

So a Document is one response for the purposes of telemetry and provenance,
and its static half never expires. The consequence to keep honest: a Document
*changes capability* partway through its life — `supports("interact")` goes
from True to False — so capability is a runtime property of the backing, not a
fact fixed at resolve time. Every capability check and the
`UnsupportedOperation` message must read live state.

---

## 3. Core API — one interface, async as a pass-through

There is exactly one public client and it reads synchronously. Properties are
properties; nothing is awaited:

```python
from webclient import WebClient

with WebClient() as wc:
    doc = wc.resolve("https://example.com")
    el  = doc.select(".price")
    print(el.attr("text"))
```

The engine underneath is async (one loop, one anyio blocking portal). Every
op is declared once and has an async implementation; the sync surface is
generated from the same registry, so there is no hand-maintained twin to
drift. **There is no `webclient.aio` to import.**

When you are already inside an event loop — a FastAPI handler, the service
layer, an async worker — you reach the async form through the object you
already hold, rather than through a parallel import:

```python
@app.post("/scrape")
async def scrape(url: str):
    doc = await wc.core.resolve(url)         # same op, awaited
    return {"markdown": await doc.core.render("markdown")}
```

`.core` is a thin async view generated from the same op registry. It exists
for the low-level path; it is not the advertised interface. Calling the sync
surface from inside a running loop raises immediately with a message pointing
at `.core`, rather than deadlocking.

**Cost, stated plainly.** Each imperative op on a browser backing is one
portal hop plus one CDP round trip, so `select_all(".row")` followed by
`.attr("text")` on 200 rows is 200 of each. That is the same shape playwright's own
sync API has. The answer is not to make the interface awkward — it is that
bulk extraction belongs in a plan (§6), which traverses once. Imperative is
for exploration and small jobs; plans are for volume, and the interface makes
that division obvious.

---

## 4. Core API — extending the render registry

The only extension point most users touch:

```python
from webclient import WebClient

def readability(doc, **opts) -> str:
    ...

wc = WebClient()
wc.renderers.register(kind="html", format="article", fn=readability)

wc.resolve(url).render("article")   # same op, plugin format
```

No `Plugin` base class, no `surfaces=[...]`, no attach/detach lifecycle — a
registry keyed on `(kind, format)` holding a function.

---

## 5. Core API — telemetry without a bus

```python
doc = wc.resolve("https://shop.example.com", browser=True)

doc.telemetry.requests        # [RequestRecord(url=..., status=..., ms=...), ...]
doc.telemetry.redirects       # the hop chain that led to final_url
doc.telemetry.console         # [ConsoleLine(level="error", text=...), ...]

# streaming consumers, dispatched by class — no topics, no correlation ids
wc.on(RequestRecord, lambda r: metrics.timing(r.host, r.ms))
```

The backing writes these fields directly. There is no subscription to
establish before navigation and none to tear down after.

---

## 6. Lazy API — the shape the whole design exists for

An e-commerce listing, extracted declaratively:

```python
from webclient import doc, el, field

plan = (
    doc.select_all(".product-card").map(
        title = el.select("h3").attr("text"),
        price = el.select(".price").attr("text").on_error("null"),
        link  = el.select("a").attr("href"),
        badge = el.select(".badge").attr("text").on_error("null"),
    )
    .require("title", "link")
    .filter(field("price") != "")
)

rows = plan.collect(wc.resolve("https://shop.example.com/laptops"))
# [{"title": "X1 Carbon", "price": "$1,299", "link": Reference(...), "badge": None}, ...]
```

`el` is the current element inside `map`; `doc` is the document being mapped
over. **This is a change from the draft**, which used `doc.select(...)` for
both — there, the same name meant "the document" outside `map` and "the
current card" inside it. Two roots, two meanings, no ambiguity.

---

## 7. Lazy API — rooted plans need no runtime context

```python
from webclient import ref

plan = (
    ref("https://shop.example.com/laptops").resolve()
      .select_all(".product-card").map(
          title = el.select("h3").attr("text"),
          link  = el.select("a").attr("href"),
      )
)

rows = plan.collect()            # no argument — the plan carries its source
```

And rooted at an already-resolved document:

```python
plan = doc("a1b2c3d4").select_all(".row").map(cell=el.select("td").attr("text"))
rows = plan.collect(client=wc)   # resolves the id in wc's document registry
```

Three source kinds, one union in the IR: `ContextSource` (bare `doc`, needs a
runtime context), `ReferenceSource(url)`, `DocumentSource(id)`.

---

## 8. Lazy API — following links (the fan-out case)

```python
plan = (
    ref("https://jobs.example.com").resolve()
      .select_all(".job").map(
          title = el.select("h2").attr("text"),
          link  = el.select("a").attr("href"),
      )
      .then(
          salary   = field("link").resolve().select(".salary").attr("text").on_error("null"),
          posted   = field("link").resolve().select("time").attr("datetime"),
          company  = field("link").resolve().select(".company").attr("text"),
      )
      .require("title")
)

for row in plan.collect(stream=True):
    print(row["title"], row["salary"])
```

Three `field("link").resolve()` chains against the same URL must compile to
**one** fetch — the scheduler dedupes identical sub-plans within a row and the
three extractions become dependents of one http step.

**Friction.** That dedupe is invisible magic. The explicit alternative reads
better and needs no cleverness:

```python
      .then(detail = field("link").resolve())
      .then(
          salary  = field("detail").select(".salary").attr("text").on_error("null"),
          posted  = field("detail").select("time").attr("datetime"),
      )
```

...but it puts a Document in a row, which conflicts with "rows carry scalars".
Proposal: allow Documents/Elements in intermediate columns, and have
`collect()` drop non-scalar columns from the output unless `keep=` names them.
Decision owed.

---

## 9. Lazy API — browser steps inside a plan

```python
plan = (
    ref("https://app.example.com/reports").resolve(browser=True)
      .click("#load-all")
      .wait_stable()
      .select_all("tr.report").map(
          name = el.select("td.name").attr("text"),
          size = el.select("td.size").attr("text"),
      )
)
```

`resolve(browser=True)`, `click` and `wait_stable` all declare
`resource="page"` and the same page group, so the scheduler serialises them on
one lease and releases it when the last dependent step completes. Pure steps
(`select_all`, `text`) run inline.

Per-row browser work is where the old executor's lack of a real scheduler hurt
most:

```python
      .then(chart = field("link").resolve(browser=True)
                        .wait_stable()
                        .screenshot(".chart"))
```

Each row's chain gets its own page lease; concurrency is bounded by
`pool.max_pages`, and rows queue rather than materialising N tasks.

---

## 10. Lazy API — nested collections

```python
plan = doc.select_all(".category").map(
    name  = el.select("h2").attr("text"),
    items = el.select_all(".item").map(              # nested map -> list column
        label = el.select(".label").attr("text"),
        sku   = el.select(".sku").attr("text"),
    ),
)

rows = plan.collect(document)
# [{"name": "Coffee", "items": [{"label": ..., "sku": ...}, ...]}, ...]

flat = plan.explode("items").collect(document)
# [{"name": "Coffee", "label": ..., "sku": ...}, ...]
```

The current engine silently drops the second `map` and returns the first
map's rows. Under the new IR a nested `Map` is a declared node with declared
cardinality; `explode` is the only way to flatten and there is no path where
a step is skipped without an error.

---

## 11. Lazy API — inspect, serialise, re-run

```python
plan.explain()
```

```
source: reference https://shop.example.com/laptops
  resolve()                          [http lease]
  select_all(".product-card")        [pure, many]
  map                                [many -> records]
    title  = select("h3").attr("text")              [pure]
    price  = select(".price").attr("text")         [pure, on_error=null]
    link   = select("a").attr("href")             [pure]
  require(title, link)
  filter(field("price") != "")
```

```python
blob = plan.to_plan().model_dump_json()      # versioned, typed IR
Plan.model_validate_json(blob).collect(client=wc)
```

Serialisation is not a bolt-on: the IR *is* the plan representation, and the
in-process path validates the same structure the wire path does.

---

## 12. Remote API — not a parallel class hierarchy

Because Elements are addresses and plans are serialisable, "remote" is a
**backing plus an execution location**, not a second API:

```python
from webclient import WebClient

wc = WebClient(remote="https://browser.internal", token=TOKEN)

doc = wc.resolve("https://shop.example.com", browser=True)   # runs server-side
doc.render("markdown")               # rendered server-side, string comes back
doc.click(".next").wait_stable()     # one round trip per op
rows = plan.collect(doc)             # plan ships whole; rows come back
```

Same `WebClient`, same `Document`, same plan objects. The old design's
`RemoteWebClient` / `RemoteDocument` / `RemoteSession` duck-typed a parallel
tree and admitted (ISSUES #37) that parity was "structural, not nominal" and
the imperative surface "best-effort and chatty".

The chattiness is real and honest here: imperative ops are one round trip
each, which is exactly the argument for pushing work into a plan. The
interface makes that visible rather than hiding it.

```python
wc.resolve(url).select_all(".card")      # N+1 round trips — bad, and obvious
plan.collect(client=wc)                  # one round trip — the fast path
```

**Friction.** Remote is *deferred*, but this shape has to be plausible now or
the address-based Element and serialisable IR lose their main justification.
The open question is whether remote imperative ops should exist at all, or
whether a remote client should accept only plans and renders.

---

## 13. The one-shot convenience (firecrawl / spider shaped)

Composition, not new machinery:

```python
wc.scrape("https://example.com", formats=["markdown", "links"])
# {"markdown": "...", "links": [...], "metadata": {...}}

wc.scrape("https://app.example.com", browser=True,
          actions=[Click("#accept"), WaitStable()],
          formats=["markdown"])
```

`scrape` = `resolve` + optional actions + render + release, in ~15 lines.

---

## Decisions this exercise surfaced

1. ~~`el` vs `doc` as the in-`map` root~~ — **settled**: two roots, `doc` for
   the document and `el` for the current element (§6). `resolve` is the only
   verb for producing a Document; `fetch` is gone.
2. ~~`doc.text` vs a `text` rendering~~ — **settled**: no `.text`/`.html`
   properties at all; `attr(name)` is one op over real and pseudo-attributes
   (`text`, `html`, `title`). The collision dissolves (§1).
3. ~~Async property asymmetry~~ — **settled**: one sync-reading interface,
   async reachable as `.core` pass-through, no `webclient.aio` import (§3).
4. ~~`navigate()` invalidating the old Document~~ — **settled**: new Document
   takes the lease; the old stays readable from its snapshot and raises
   `StaleDocument` only on live ops. Capability is therefore runtime state (§2).
5. ~~Sub-plan dedupe vs Documents-in-columns~~ — **settled**: Documents may
   sit in intermediate columns; `collect()` drops non-scalar columns unless
   `keep=` names them. No invisible dedupe (§8).
6. Whether remote supports imperative ops or plans/renders only (§12).
