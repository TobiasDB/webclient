# Interface by example

Worked use cases for the core, written *before* implementation so the
interface could be judged on how it reads, then reconciled against the built
code. **§1–§11 and §13 are implemented and exercised by `demo.py` and the test
suite; §12 (remote) is deferred.** Where a section says a decision was owed,
the decision is now recorded at the end.

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
    doc.fields               # {} — extractions accumulate here (§6)

    card = doc.select(".post", index=0)
    card.attr("text")               # Value[str] — see below
    card.select("h2").attr("text")  # "Hello world"
    card.attr("href")               # -> Reference (link attrs narrow to Reference)
    card.attr("data-id")            # -> str
    card.attr("data-id", optional=True)   # -> str | None
```

**One accessor, not three.** There is no `.text` property, no `.html`
property, and no naming collision with a `text` *rendering*: `attr(name)` is a
single op covering real attributes and pseudo-attributes alike.

```python
    el.attr("text")     # normalised text content   -> Value[str]
    el.attr("html")     # outer HTML                -> Value[str]
    el.attr("href")     # link attrs narrow         -> Reference
    el.attr("data-id")  # any real attribute        -> Value[str]
    el.attr("nope")     # LookupError, or optional=True -> Value[None]

    el.attr("href").resolve()      # so this is a complete thought
    el.attr("text").get()          # "Hello world" — explicit unwrap
    print(el.attr("text"))         # __str__, no unwrap needed
    el.attr("text") == "Hello"     # True — eager comparison
```

Documents accept the same op against their root, so `doc.attr("title")` and
`doc.attr("text")` need no separate spellings. `doc.content` stays the raw
bytes and the render surface keeps its own verb, `render(format)` (`markdown`, `readable`, `links`,
`elements`, `links`), which now cannot be confused with an accessor.

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

## 6. One expression language, two evaluation modes

`then` / `map` / `otherwise` / `filter` are **methods on the real classes**,
not a parallel lazy API. Called on a resolved Document they evaluate now;
called on the module-level roots (`doc`, `el`, `ref`, which are the same
classes with `is_lazy=True`) they record.

```python
document = wc.resolve("https://shop.example.com/laptops")

# eager: evaluates immediately, returns the record
record = document.then(
    doc.select("h1").attr("text").alias("category"),
    rows = doc.select_all(".product-card").map(
        el.select("h3").attr("text").alias("title"),
        price = el.select(".price").attr("text"),
    ),
)
```

The arguments are expressions either way — they are built from the `doc`/`el`
roots, which are lazy by construction. The only difference is whether the
receiver is a real document or a lazy one. **So there is no generated
`LazyDocument`, no `.pyi` to keep in sync, and no codegen step**: eager and
lazy are one class and one set of signatures, and cannot drift because they
are literally the same method.

### Extractions are stored on the Document

An eager projection is remembered, and `field()` reads it back:

```python
document.then(title = doc.select("h1").attr("text"))
document.fields                  # {"title": "Laptops"}
document.field("title")          # "Laptops"

document.then(count = doc.select(".n").attr("text"))
document.fields                  # {"title": "Laptops", "count": "48 results"}
```

`field(name)` therefore means one thing in both modes: *a value already
extracted in this context*. In a plan that is the record being built; on a
Document it is `doc.fields`. This is what makes `field("link").resolve()`
(§8) read the same whether the plan runs now or later.

### What ops return: `Value[T]` and `Selection[T]`

For one class to be honest in both modes, ops return wrappers rather than raw
values: `attr -> Value[str]`, `select -> Selection[Element]`. Eagerly a
wrapper is already materialised; lazily it is unevaluated. Either way
`.alias()`, `.otherwise()`, `.map()` and `.filter()` are visible to the type
checker, which is what makes plan authoring type-check at all — declaring
`attr -> str` would make `.alias()` an error on `str` in the single most
common idiom in the language.

```python
title = document.select("h1").attr("text")   # Value[str]
print(title)                                  # __str__
f"{title}"                                    # __format__
title == "Laptops"                            # True, eagerly
title.get()                                   # "Laptops" — explicit unwrap
title.get().upper()                           # str methods go through .get()
```

The tax is `.get()` on the eager path when you need the raw object. In
exchange there is one class, one set of signatures, honest typing in both
modes, and no generated code anywhere.

---

## 6b. The two record constructors

The language is expressions plus **record constructors**, composable at any
depth. There are exactly two constructors:

- `then(...)` — one context in, **one record** out (projection)
- `map(...)` — many contexts in, **many records** out (`then` lifted over a
  collection)

Both take positional expressions named with `.alias()`, and keyword
expressions. Positional + alias exists because a column name is not always a
Python identifier, and because a long expression reads better before its name
than after it.

```python
from webclient import ref, doc, el

plan = (
    ref("https://shop.example.com/laptops").resolve()
    .then(
        doc.select("h1").attr("text").alias("category"),
        count = doc.select(".result-count").attr("text"),
        rows  = doc.select_all(".product-card").map(
            el.select("h3").attr("text").alias("title"),
            price = el.select(".price").attr("text"),
            link  = el.select("a").attr("href"),
        ),
    )
)

plan.collect()
```

```python
{"category": "Laptops",
 "count": "48 results",
 "rows": [{"title": "X1 Carbon", "price": "$1,299", "link": Reference(...)},
          {"title": "T14",       "price": "$980",   "link": Reference(...)}]}
```

The result is a **tree, not a table** — which is what firecrawl-shaped
consumers actually want, and what the old flat-rows model could not express.
`.explode("rows")` flattens where a tabular consumer needs it.

`filter` survives as a predicate over a collection; it is about *unwanted*
data, not *failed* data, so it stays distinct from `otherwise`:

```python
    rows = (doc.select_all(".product-card")
              .filter(el.select(".price").attr("text") != "")
              .map(...))
```

---

## 7. Lazy API — rooted plans need no runtime context

```python
plan = ref("https://shop.example.com").resolve().then(title=doc.attr("title"))
plan.collect()                       # no argument — the plan carries its source

plan = doc("a1b2c3d4").then(rows=doc.select_all(".row").map(...))
plan.collect(client=wc)              # rooted at a resolved document id

plan = doc.then(rows=doc.select_all(".row").map(...))
plan.collect(some_document)          # ambient context
```

Three source kinds, one union in the IR: `Reference(url)`, `Document(id)`,
`Context`.

---

## 8. Lazy API — `otherwise` recovers, it does not just null out

This is the case the whole design has to get right: fan out over listings,
follow each link, and when a detail page fails, **keep what the failure told
you** instead of a null.

```python
plan = (
    ref("https://jobs.example.com").resolve()
    .then(
        doc.select("h1").attr("text").alias("board"),
        rows = doc.select_all(".job").map(
            el.select("h2").attr("text").alias("title"),
            link   = el.select("a").attr("href"),
            detail = field("link").resolve().then(
                salary  = doc.select(".salary").attr("text"),
                posted  = doc.select("time").attr("datetime"),
                company = doc.select(".company").attr("text"),
            ).otherwise(
                status  = doc.status_code,   # 404, or 0 if never responded
                body    = doc.attr("text"),  # the error page, if there was one
                message = err.message,       # the failure itself
                where   = err.op,            # "select('.salary')"
            ),
        ),
    )
    .otherwise(RAISE_ERROR)
)
```

```python
{"board": "Engineering",
 "rows": [
   {"title": "Backend", "link": Reference(...),
    "detail": {"ok": True,
               "salary": "£90k", "posted": "2026-09-01", "company": "Acme"}},
   {"title": "Frontend", "link": Reference(...),
    "detail": {"ok": False,
               "status": 404, "body": "Not Found", "message": "HTTP 404",
               "where": "resolve()"}},
 ]}
```

`otherwise` takes **either** a recovery projection **or** a sentinel:

| Form | Meaning |
|---|---|
| `.otherwise(a=…, b=…)` | on failure, produce this record instead — **tagged** with `ok` |
| `.otherwise(RAISE_ERROR)` | on failure, abort the whole plan |
| `.otherwise(DROP_ROW)` | on failure, drop the enclosing row |
| `.otherwise(NULL)` | on failure, yield null for this field |

Only a recovery projection tags; a sentinel leaves the value alone.

That single combinator absorbs the whole error model — the earlier design
needed `on_error(policy)` **and** `require(*fields)` **and** a plan-level
strictness flag to say less than this does.

Note the resolve is written **once**. The earlier draft needed either
invisible sub-plan dedupe or a Document parked in a column; here the resolved
document is simply the context of the `then` block that projects it, so it
never has to be named or stored. **This supersedes the earlier
Documents-in-columns decision.**

**Two roots inside `otherwise`.** `doc` is the context reached at the point
of failure — a real not-ok Document for a 404, a synthetic one with
`status_code == 0` when nothing ever responded. `err` is the failure itself
(`err.message`, `err.op`, `err.kind`). Both are needed because a 404 and a
DNS failure are genuinely different events: the first has a body worth
keeping, the second has only an error.

**Recovered columns are tagged.** The column carries which arm produced it,
so a consumer branches on `ok` instead of sniffing which fields came back
null — and the two arms are free to have unrelated shapes, which they
normally do. Failures are countable without a schema comparison:

```python
failed = [r for r in result["rows"] if not r["detail"]["ok"]]
```

---

## 9. Lazy API — browser steps inside a plan

```python
plan = (
    ref("https://app.example.com/reports").resolve(browser=True)
      .click("#load-all")
      .wait_stable()
      .then(rows = doc.select_all("tr.report").map(
          el.select("td.name").attr("text").alias("name"),
          size  = el.select("td.size").attr("text"),
          chart = el.select("a").attr("href").resolve(browser=True)
                    .wait_stable()
                    .then(shot = doc.screenshot(".chart"))
                    .otherwise(NULL),
      ))
)
```

`resolve(browser=True)`, `click` and `wait_stable` declare `resource="page"`
and share a page group, so the scheduler serialises them on one lease and
releases it when the last dependent step completes. Pure steps run inline.
Per-row browser work takes its own lease; concurrency is bounded by
`pool.max_pages` and rows queue rather than materialising N tasks.

---

## 10. Lazy API — nesting is the normal case

Because `then` and `map` compose, arbitrary depth needs no special
construct:

```python
plan = doc.then(rows = doc.select_all(".category").map(
    el.select("h2").attr("text").alias("name"),
    items = el.select_all(".item").map(
        el.select(".label").attr("text").alias("label"),
        sku = el.select(".sku").attr("text"),
    ),
))

plan.collect(document)
# {"rows": [{"name": "Coffee",
#            "items": [{"label": "Ethiopia", "sku": "C-1"}, ...]}, ...]}

plan.explode("rows.items").collect(document)
# [{"name": "Coffee", "label": "Ethiopia", "sku": "C-1"}, ...]
```

The current engine silently drops a second `map` and returns the first one's
rows. Under the new IR a nested `Map` is a declared node with declared
cardinality, and there is no branch anywhere that skips a step it does not
recognise.

---

## 11. Lazy API — inspect, serialise, re-run

```python
plan.explain()
```

```
source: reference https://jobs.example.com
resolve()                                       [http lease]
then
  board = select("h1").attr("text")             [pure]
  rows  = select_all(".job")                    [pure, many]
          map
            title  = select("h2").attr("text")  [pure]
            link   = select("a").attr("href")   [pure]
            detail = field("link")
                     resolve()                  [http lease, per row]
                     then
                       salary  = select(".salary").attr("text")
                       posted  = select("time").attr("datetime")
                       company = select(".company").attr("text")
                     otherwise -> record{status, message}
otherwise -> RAISE_ERROR
```

```python
blob = plan.to_plan().model_dump_json()      # versioned, typed IR
Plan.model_validate_json(blob).collect(client=wc)
```

Serialisation is not a bolt-on: the IR *is* the plan representation, and the
in-process path validates the same structure a wire path would.

---

## 12. Remote API — not a parallel class hierarchy *(deferred)*

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
6. Whether remote supports imperative ops or plans/renders only (§12) —
   **still open**, deferred with remote itself.
7. ~~Generated lazy twin + `.pyi`~~ — **superseded**: `then`/`map`/`otherwise`
   are methods on the real classes, so the lazy root is the same class with
   `is_lazy=True`. No codegen, no stub (§6).
8. ~~`on_error` + `require`~~ — **superseded**: `otherwise` takes a recovery
   projection or a sentinel and absorbs both (§8).
9. ~~Documents in intermediate columns + `keep=`~~ — **superseded**: a
   resolved document is the *context* of the `then` that projects it, so it
   never needs naming or storing (§8).
10. ~~How mode-dependent return types are declared~~ — **settled**: ops
    return `Value[T]` / `Selection[T]` wrappers, honest in both modes;
    `.get()` unwraps on the eager path (§6).
11. ~~The root inside `otherwise`~~ — **settled**: two roots, `doc` for the
    context reached and `err` for the failure (§8).
12. ~~Recovered column schema~~ — **settled**: tagged records carrying
    `ok` (§8).
