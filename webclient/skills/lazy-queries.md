---
name: lazy-web-queries
description: >-
  Write lazy extraction queries (plans) that pull structured data out of a web
  page. Covers the whole query DSL — the `wq` roots, select/select_all/attr/
  text_content, extract/filter/project, the operators, and portable blobs. This is
  the query syntax only: you do not need to know how the page is fetched, rendered,
  or which client runs the plan.
---

# Writing lazy web queries

A **lazy query** records a *plan* — a chain of steps. Nothing runs while you build
it; the plan executes only when something collects it (against a page as context).
The same plan you write here runs locally, async, or over the wire, and serialises
to a short blob. Your job is to write the plan; how and where it runs is not your
concern.

## The one rule that matters

**A lazy value records, it does not run.** So never use Python's control words on
one — no `and` / `or` / `not`, no `bool()`, `len()`, `if`, or `for`. Use the
operators and the row-shaping ops below instead. (`text_content == "In stock"`
records a comparison step; `if text_content == ...` would try to run it and fail.)

## Roots — start every plan from `wq`

```python
from webclient import wq
```

| root | is | use it for |
|---|---|---|
| `wq.ref` | the page/reference the plan is run against | a whole "resolve → extract" plan |
| `wq.doc` | the current element (inside `extract` / `filter`) | per-row / per-element sub-queries |
| `wq.field("col")` | an already-extracted column value | referencing an earlier column |
| `wq.reference("col")` | a column that holds a link (`Reference`) | following a link you extracted |

`wq.ref.resolve()` turns the run context into the page; from there you select and
shape. `wq.doc` is how you reach *into* each row while shaping it.

## Building blocks

Chain these; each returns a new lazy node.

- `.select("css or xpath")` → the first match, as a sub-element (selection nests).
  A miss raises — pass `optional=True` (or `error=RETURN`) to get a not-ok node instead.
- `.select_all("css")` → a collection of every match (an empty match is still a collection).
- `.attr("href" | "src" | "action")` → a link `Reference` (resolvable); any other
  attribute → a `Field` (its value is `.value`, or use it directly in a comparison).
- `.text_content` → the element's text. **A property — no parentheses.**
- `.markdown()` / `.text()` / `.links()` / `.elements()` → rendered forms of an element.
- `.extract(col=expr, …)` → attach columns to each element in a collection; each
  `expr` is a `wq.doc…` sub-query evaluated on that element.
- `.filter(pred, …)` → keep the elements where every predicate is truthy.
- `.project()` → materialise to `list[dict]`. `.project(Model)` → `list[Model]`
  (a pydantic model; eager-only — see gotchas).
- `.is_ok()` / `.is_empty()` → lazy booleans (use with `filter`).

## Operators (not keywords)

Comparisons and logic on lazy values use symbols:

`==` `!=` `<` `<=` `>` `>=` for comparison; `&` (and) `|` (or) `~` (not) to combine.
Wrap each side of `&`/`|` in parentheses: `(a) & (b)`.

## Examples

### Rows → list of dicts
```python
from webclient import wq

plan = (
    wq.ref.resolve()
    .select_all(".item")                                  # one node per row
    .extract(
        title=wq.doc.select(".title").text_content,
        price=wq.doc.select(".price").text_content,
        url=wq.doc.select("a").attr("href"),              # a Reference
    )
    .project()                                            # -> list[dict]
)
```

### Rows → a typed model
```python
from pydantic import BaseModel
from webclient import wq

class Product(BaseModel):
    title: str = ""
    price: str = ""

plan = (
    wq.ref.resolve()
    .select_all(".item")
    .extract(title=wq.doc.select(".title").text_content,
             price=wq.doc.select(".price").text_content)
    .project(Product)                                     # -> list[Product]
)
```

### Filter rows
```python
# keep in-stock rows (reference a column you extracted with wq.field)
plan = (
    wq.ref.resolve()
    .select_all(".item")
    .extract(title=wq.doc.select(".title").text_content,
             in_stock=wq.doc.select(".status").text_content == "In stock")
    .filter(wq.field("in_stock"))
    .project()
)

# keep rows WITHOUT a `.sold-out` badge (optional select + ~ + is_ok)
plan = (
    wq.ref.resolve()
    .select_all(".item")
    .filter(~wq.doc.select(".sold-out", optional=True).is_ok())
    .project()
)
```

### Follow a link you extracted
`attr("href")` yields a `Reference`; `wq.reference("col")` follows a column holding one.
```python
plan = (
    wq.ref.resolve()
    .select_all(".item")
    .extract(link=wq.doc.select("a").attr("href"))
    .extract(name=wq.doc.reference("link").resolve().select("h1").text_content)
    .project()
)
```

### Hand a plan off as a blob
A plan serialises to a short JSON blob; it can be rebuilt, name-validated, and
pretty-printed before it runs.
```python
from webclient import from_blob

blob = plan.to_blob()          # a compact JSON string — safe to store or send
plan.explain()                 # readable: Reference.resolve().select_all('.item')…
from_blob(blob)                # rebuild it elsewhere, then run against a page
```

## Gotchas

- `text_content` / `title` are **properties** — no `()`. `attr(...)`, `select(...)`,
  `markdown()`, `project()` are calls.
- Every op that can miss (`select`, `attr`, `click`, `write`, `wait_for`) is loud by
  default. Pass `optional=True` (or `error=RETURN`) for a not-ok node you can branch
  on with `.is_ok()`; otherwise a miss raises.
- `select` scopes to the current element, so `.select_all(".item")` then
  `wq.doc.select(".title")` targets the title *within that row*.
- `.project(Model)` is eager-only — a model class is not part of the serialisable
  plan. Project to plain dicts in a portable plan, or validate the dicts into a
  model after the plan runs.
