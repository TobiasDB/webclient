---
name: lazy-web-queries
description: >-
  Spec and examples for authoring lazy extraction queries — plans that pull
  structured data out of a web page. Covers the whole query DSL: the `wq` roots,
  select/select_all/attr/text_content, extract/filter/project, the operators, and
  portable blobs. Query syntax only — nothing about fetching, rendering, or running.
---

# Lazy web queries — spec & examples

A **query** is a *plan*: a recorded chain of steps. Building it runs nothing; the
plan executes only when a runner collects it against a page. You are given one
namespace, `wq`, to build from. That is the entire surface you need.

## The one rule

**A lazy value records, it does not run.** Never use Python control words on one —
no `and` / `or` / `not`, no `bool()`, `len()`, `if`, or `for`. Use the operators and
ops below. (`x == "In stock"` records a comparison; `if x == "In stock"` tries to
*run* the recording and fails.)

## Roots

| root | is |
|---|---|
| `wq.ref` | the page/context the plan is run against |
| `wq.doc` | the current element — used inside `extract` / `filter` |
| `wq.field("col")` | the value of a column already extracted |
| `wq.reference("col")` | a column that holds a link (to follow with `.resolve()`) |

## Steps (the grammar)

**Selection** (on a page or element; selection nests):
- `.select("css | xpath")` → the first match, as an element. A miss raises; pass
  `optional=True` (or `error=RETURN`) for a not-ok element instead. `index=` picks
  the n-th match.
- `.select_all("css")` → a collection of every match (an empty match is still a
  collection). `limit=` / `offset=` bound it.

**Values** — `.attr(name)` is the one accessor: "give me `name` from this element".
- `.attr("text")` → the element's **text**. (`.text_content` is the same thing.)
- `.attr("html")` → the element's markup.
- `.attr("href" | "src" | "action")` → a **link** (resolvable / has `.url`).
- `.attr(other)` → the HTML attribute named `other` (`class`, `data-id`, …), as a
  field whose `.value` is the string. Add `optional=True` for one that may be absent.
- `.markdown()` / `.text()` / `.links()` / `.elements()` → rendered forms.

**Navigation:**
- `.resolve()` turns a link (or `wq.ref`) into its page, so you can select into it.

**Shaping** (on a collection):
- `.extract(col=expr, …)` → attach columns to each element; each `expr` is a
  `wq.doc…` sub-query evaluated on that element.
- `.filter(pred, …)` → keep elements where every predicate is truthy.
- `.project()` → materialise to a list of dict rows. `.project(Model)` → a list of
  a model class you pass.
- `.is_ok()` / `.is_empty()` → lazy booleans, for use in `filter`.

**Portability** (on a plan):
- `.to_blob()` → the plan as a short JSON string (store or hand off).
- `.explain()` → a readable one-line rendering of the recorded chain.

## Operators (symbols, never keywords)

Comparison: `==` `!=` `<` `<=` `>` `>=`. Logic: `&` (and) `|` (or) `~` (not) —
parenthesise each side: `(a) & (b)`.

## Behaviour

- **Records, never runs.** A step returns a new lazy node; nothing evaluates until
  the plan is collected. So the operators above — not `and`/`or`/`not`/`bool()` —
  and no `if`/`for`/`len()` on a lazy value.
- **Loud by default, everywhere.** Every op that can miss (`select`, `attr`, …)
  raises on a miss -- and this holds INSIDE `extract` / `filter` too: a column or
  predicate whose select misses aborts the run (naming the selector), never a silent
  `None`. Pass `optional=True` (or `error=RETURN`) on that select for a genuinely
  optional field -> a not-ok result you branch on with `.is_ok()` / `.is_empty()`.
- **Selection nests and scopes.** A selected element is itself selectable, and a
  sub-query scopes to it: after `.select_all(".item")`, `wq.doc.select(".title")`
  targets the title *within that row*, not the whole page.
- **Properties vs calls.** `attr(...)`, `select(...)`, `markdown()`, `project()` are
  calls; `text_content` / `title` are properties (no `()`).
- **`extract` is per-element.** Each column expr is evaluated on the current element
  (`wq.doc`), once per element in the collection.
- **`project(Model)` is eager-only.** A model class is not part of a portable blob;
  project to dict rows in a blob and validate into a model after the plan runs.

## Choosing stable selectors

A selector is only as good as it is durable — pages get re-styled and re-ordered.
Prefer hooks that describe *what* a node is over *where* it sits or how it looks.

Prefer, best first:
1. **Purpose-built test/id hooks:** `#id`, `[data-testid=…]`, `[data-test=…]`,
   `[data-qa=…]`, `[data-cy=…]` — added for automation, rarely change.
2. **Semantic attributes / microdata:** `[itemprop=price]`, `[role=…]`,
   `[aria-label=…]`, `[name=…]`, `[type=…]`, and semantic elements
   (`article`, `nav`, `main`, `time`, `address`).
3. **Meaningful, human-named classes:** `.product-card`, `.price`, `.byline` —
   names that describe content, not styling.

Avoid — these break on any redesign:
- **Hashed / generated classes:** `.css-1a2b3c`, `.Button_x7Kd`, `.sc-bdVaJa` (CSS
  modules / styled-components). Match the stable part instead: `[class*="price"]`.
- **Utility classes:** `.mt-4`, `.flex`, `.text-sm` (Tailwind & co.) — they mark
  layout, not content, and repeat everywhere.
- **Deep positional chains:** `div > div:nth-child(3) > span` — one inserted `<div>`
  and it's wrong.
- **Tag-only selectors:** `span`, `a` — too broad; they grab the wrong node.

Techniques:
- **Anchor on a stable ancestor, then a semantic leaf:** `.product-card .price`
  scopes a common leaf to the right container. With `select_all` + `extract`, select
  the row on a stable container class and the fields relative to it (`wq.doc`).
- **`nth-child` / `nth-of-type` only for truly uniform, order-stable lists** (e.g.
  table columns), never to reach into hand-built markup.
- **Attribute *contains* for partial-stable classes:** `[class*="teaser"]`,
  `[href*="/product/"]`.
- **Use XPath when you must match on text** the CSS can't express, e.g.
  `//button[normalize-space()="Add to cart"]`.
- **Verify breadth:** a `select_all` should match exactly the set you mean — too many
  hits means the selector is too broad, zero means too specific.

## Examples

Rows → list of dicts:
```python
(
    wq.ref.resolve()
    .select_all(".item")
    .extract(
        title=wq.doc.select(".title").text_content,
        price=wq.doc.select(".price").text_content,
        url=wq.doc.select("a").attr("href"),
    )
    .project()
)
```

Rows → a typed model (pass any model class to `project`):
```python
(
    wq.ref.resolve()
    .select_all(".item")
    .extract(title=wq.doc.select(".title").text_content,
             price=wq.doc.select(".price").text_content)
    .project(Product)
)
```

Filter — keep in-stock rows (reference an extracted column with `wq.field`):
```python
(
    wq.ref.resolve()
    .select_all(".item")
    .extract(title=wq.doc.select(".title").text_content,
             in_stock=wq.doc.select(".status").text_content == "In stock")
    .filter(wq.field("in_stock"))
    .project()
)
```

Filter — keep rows WITHOUT a `.sold-out` badge (optional select + `~` + `is_ok`):
```python
(
    wq.ref.resolve()
    .select_all(".item")
    .filter(~wq.doc.select(".sold-out", optional=True).is_ok())
    .project()
)
```

Follow a link you extracted (`wq.reference` follows a column holding a link):
```python
(
    wq.ref.resolve()
    .select_all(".item")
    .extract(link=wq.doc.select("a").attr("href"))
    .extract(name=wq.doc.reference("link").resolve().select("h1").text_content)
    .project()
)
```

Serialise / inspect a plan:
```python
plan.to_blob()    # -> a compact JSON string, portable and safe to store or send
plan.explain()    # -> "Reference.resolve().select_all('.item').extract(...)"
```
