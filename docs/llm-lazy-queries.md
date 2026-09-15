# Building lazy extraction queries (an LLM guide)

This guide is for an LLM (or an agent) writing **lazy queries** to extract
structured data from a web page with `webclient`. A lazy query *records* a plan;
nothing runs until you `.collect()` it. The same plan runs locally, async, or over
the wire, and serialises to a short blob you can hand off.

## The 30-second model

1. **See the structure cheaply.** Fetch the page and read its *skeleton* — a
   token-lean `tag#id.class` outline — so you can write CSS selectors without the
   raw HTML.
2. **Write a plan** from the `wq` roots (`wq.doc`, `wq.ref`, …). Chain
   `select` / `select_all` / `attr` / `text_content`, shape rows with
   `extract` / `filter` / `project`.
3. **Run it** with `.collect()` (or `.acollect()` / `.stream()`), optionally
   against a context (`plan.collect(wc.ref(url))`).

```python
from webclient import WebClient, wq

with WebClient() as wc:
    page = wc.fetch("https://shop.example/")
    print(page.skeleton())        # <-- read this first; write selectors from it
```

A skeleton looks like:

```
# skeleton: tag#id.class[attr=val]  ×N=N identical siblings  "…"=sample text
main#main.container
  ul#results.list
    li.item ×20
      span.title  "Aeropress"
      a.link[href]  "view"
      span.price  "$39"
  form
    input[type=text][name=q][placeholder=Search]
```

**Reading it** (each line is one element, indentation = nesting):
- `tag#id.class.class` is a ready CSS selector — e.g. `ul#results.list`, `.item`,
  `.item .price`. Key attributes are shown as `[type=…]` / `[name=…]` /
  `[placeholder=…]` / `[role=…]` / `[data-testid=…]`, and `[href]` / `[src]` mark a
  link/media target.
- `×20` means 20 **structurally-identical** siblings (a uniform list) — collapsed to
  one line; a differently-shaped sibling is *never* merged away, so what you see is
  the true shape.
- `"…"` is a short text sample from a leaf, so you can tell content nodes apart.

From that you write selectors immediately: `.item`, `.item .title`, `.item .price`,
`.item a`, `input[name=q]`.

**SPA / dynamic pages.** Fetch with `browser="probe"` (renders JS *and* compares to
the static HTML). The skeleton then marks nodes that were **not** in the server's
initial HTML as `[xhr]` (if the page fetched data) or `[js]`, and lists the data
APIs it called — so you know what is server-initial vs client-loaded, and which
JSON endpoints to hit directly:

```python
d = wc.fetch(url, browser="probe")
print(d.skeleton())
# # XHR/fetch data APIs: https://site/api/items
# div#app
#   ul.list [xhr]
#     li.item [xhr] ×20  "Aeropress"     <- injected client-side from the API above
```

## The roots (`wq`)

Import the authoring roots and build plans off them. Every attribute/call returns
a new lazy node; nothing executes.

| root | rooted at | use for |
|---|---|---|
| `wq.doc` | the element being shaped (inside `extract`/`filter`) | per-row/element sub-queries |
| `wq.ref` | a `Reference` you supply as the run context | a whole fetch→extract plan |
| `wq.field("col")` | an already-extracted column | referencing an earlier column |
| `wq.reference("col")` | a `Reference` column | following a link you extracted |

```python
from webclient import wq
doc, ref, field, reference = wq.doc, wq.ref, wq.field, wq.reference
```

(They're also importable directly: `from webclient import doc, ref, field, reference, many`.)

## Building blocks

- `select(css_or_xpath)` → the first match as a sub-`Document` (selection nests).
  Misses raise; pass `optional=True` (or `error=RETURN`) for a not-ok result.
- `select_all(css)` → a `Collection` of matches (empty is still a `Collection`).
- `attr("href"|"src"|"action")` → a `Reference` (resolvable); any other attr → a `Field`.
- `text_content` → the element's text (a property — **no parentheses**).
- `markdown()` / `text()` / `links()` / `elements()` → rendered forms.
- `extract(col=expr, …)` → annotate each element with columns (evaluated per element).
- `filter(pred, …)` → keep elements where every predicate is truthy (`~`, `&`, `|` combine).
- `project()` → materialise to `list[dict]`; `project(Model)` → `list[Model]` (a pydantic model).

Comparisons and logic on lazy values use the operators, not Python keywords:
`==`, `!=`, `<`, `>`, `&` (and), `|` (or), `~` (not). Never use `and`/`or`/`not`/`bool()`.

## Concrete examples

### 1. Extract a table of rows

```python
from webclient import WebClient, wq

with WebClient() as wc:
    rows = (
        wc.fetch("https://shop.example/")
        .select_all(".item")                       # one lazy node per product
        .extract(
            title=wq.doc.select(".title").text_content,
            price=wq.doc.select(".price").text_content,
            url=wq.doc.select("a").attr("href"),    # a Reference
        )
        .project()                                  # -> list[dict]
    )
```

### 2. Project into a typed model

```python
from pydantic import BaseModel
from webclient import WebClient, wq

class Product(BaseModel):
    title: str = ""
    price: str = ""

with WebClient() as wc:
    products = (
        wc.fetch("https://shop.example/")
        .select_all(".item")
        .extract(title=wq.doc.select(".title").text_content,
                 price=wq.doc.select(".price").text_content)
        .project(Product)                           # -> list[Product]
    )
```

### 3. Filter rows (reference an extracted column with `field`)

```python
in_stock = (
    wc.fetch(url)
    .select_all(".item")
    .extract(title=wq.doc.select(".title").text_content,
             status=wq.doc.select(".status").text_content == "In stock")
    .filter(wq.field("status"))                     # keep only the truthy ones
    .project()
)

# keep rows WITHOUT a `.sold-out` badge:
available = (
    coll.filter(~wq.doc.select(".sold-out", optional=True).is_ok()).project()
)
```

### 4. Follow a link you extracted (`reference`)

`attr("href")` gives a `Reference`; `reference("col")` follows a column that holds one:

```python
detailed = (
    wc.fetch(url)
    .select_all(".item")
    .extract(link=wq.doc.select("a").attr("href"))
    .extract(name=wq.doc.reference("link").resolve().select("h1").text_content)
    .project()
)
```

### 5. A reusable plan run against a context

Build the plan from `wq.ref` (no client bound), then run it against any URL:

```python
from webclient import wq

plan = (
    wq.ref.resolve()
    .select_all(".item")
    .extract(title=wq.doc.select(".title").text_content)
    .project()
)

rows = plan.collect(wc.ref("https://shop.example/"))       # sync
# rows = await plan.acollect(ac.ref(url))                  # async
# for row in plan.stream(wc.ref(url)): ...                 # streamed, row-by-row
```

### 6. Hand a plan off as a blob

A plan serialises to a short JSON blob you can store/send; rebuild, validate and
pretty-print it before running:

```python
from webclient import from_blob

blob = plan.to_blob()                 # a compact JSON string
print(plan.explain())                 # human-readable: Reference.resolve().select_all('.item')…
rows = from_blob(blob, wc).collect(wc.ref(url))   # rebuilt + name-validated, then run
```

## Recipe: scrape an unfamiliar page

1. `page = wc.fetch(url)`
2. `print(page.skeleton())` — find the container and the fields (ids/classes).
3. Pick the row selector (e.g. `.item`) and the field selectors (`.title`, `.price`, `a`).
4. Write `select_all(row).extract(**fields).project(Model)`.
5. If a field is sometimes missing, use `optional=True` on its `select`, or a
   `filter` to drop incomplete rows.

## Gotchas

- `text_content` / `title` are **properties** — no `()`. `attr(...)`, `select(...)`,
  `markdown()` are calls.
- Leniency is uniform: `optional=True` **or** `error=RETURN` on any op that can miss
  (`select`, `attr`, `click`, `write`, `wait_for`, `search`). A miss otherwise raises
  a `WebException` (catch that one type for both fetch and selection failures).
- Don't use `and`/`or`/`not`/`bool()`/`len()`/`for` on a lazy value — use `& | ~`
  and the row-shaping ops. A lazy value records; it does not run.
- `project(Model)` is eager-only (a model class isn't part of the serialisable plan):
  call it on a materialised collection, or run the plan then validate.
