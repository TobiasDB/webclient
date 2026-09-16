---
name: lazy-web-queries
description: >-
  Spec and examples for authoring a lazy extraction query — a plan that pulls
  structured data out of ONE web document. Covers the whole DSL: the `wq.doc` root,
  select/select_all/attr/text_content/regex, extract/filter/project, the operators,
  and portable blobs. You author only the query over the document; the caller
  fetches, resolves and runs it.
---

# Lazy web queries — spec & examples

A **query** is a *plan*: a recorded chain of steps over a document. Building it runs
nothing; the plan executes only when the caller collects it against a page. You write
one thing — the extraction over the document — starting from `wq.doc`. Fetching,
rendering and resolving are already done for you: the query is handed the document.

## The one rule

**A lazy value records, it does not run.** Never use Python control words on one —
no `and` / `or` / `not`, no `bool()`, `len()`, `if`, or `for`. Use the operators and
ops below. (`x == "In stock"` records a comparison; `if x == "In stock"` tries to
*run* the recording and fails.)

## Root

You build from one root:

| root | is |
|---|---|
| `wq.doc` | the current document — the **whole page** at the top of the chain, and the **current row** inside `extract` / `filter` |
| `wq.field("col")` | the value of a column already extracted (reference it in a later column or a filter) |
| `wq.reference("col")` | a column that holds a link, to follow with `.resolve()` inside `extract` |

Do **not** write `wq.ref`, `.resolve()` on the page, or anything about fetching /
browsers — the caller supplies the resolved document. `.resolve()` appears only to
follow a link you extracted (`wq.reference(...)`), never at the start.

## Op reference (generated from the live surface)

<!-- OP-REFERENCE -->

`.attr("text")` is the same as `.text_content`; `.attr("href"|"src"|"action")` gives a
link (has `.url`, resolvable); `.attr(other)` gives that HTML attribute as a field.

## Operators (symbols, never keywords)

Comparison: `==` `!=` `<` `<=` `>` `>=`. Logic: `&` (and) `|` (or) `~` (not) —
parenthesise each side: `(a) & (b)`.

## Behaviour

- **Records, never runs.** A step returns a new lazy node; nothing evaluates until
  the plan is collected. So use the operators above — not `and`/`or`/`not`/`bool()`,
  and no `if`/`for`/`len()` on a lazy value.
- **Loud by default, everywhere.** Every op that can miss (`select`, `attr`, …)
  raises on a miss — INSIDE `extract` / `filter` too: a column or predicate whose
  select misses aborts the run (naming the selector), never a silent `None`. Pass
  `optional=True` (or `error=RETURN`) on that select for a genuinely optional field
  → a not-ok result you branch on with `.is_ok()` / `.is_empty()`.
- **Selection nests and scopes.** A selected element is itself selectable, and a
  sub-query scopes to it: after `.select_all(".item")`, `wq.doc.select(".title")`
  targets the title *within that row*, not the whole page.
- **Properties vs calls.** `select(...)`, `attr(...)`, `regex(...)`, `project()` are
  calls; `text_content` is a property (no `()`).
- **`extract` is per-element.** Each column expr is evaluated on the current element
  (`wq.doc`), once per element in the collection.
- **Nest a sub-`extract` for a structured field.** A column whose value is itself
  `wq.doc.select(...).extract(...).project()` produces nested JSON (e.g. a `price`
  object with `value` / `unit`).
- **`regex` splits a messy string.** `wq.doc.select(".price").regex(r"[\d.]+")` pulls
  the number out; `group=1` picks a capture group.
- **`project(Model)` is eager-only.** A model class is not part of a portable blob;
  project to dict rows in a blob and validate into a model after the plan runs.

## Choosing stable selectors

A selector is only as good as it is durable — pages get re-styled and re-ordered.
Prefer hooks that describe *what* a node is over *where* it sits or how it looks.

Prefer, best first:
1. **Purpose-built test/id hooks:** `#id`, `[data-testid=…]`, `[data-test=…]`,
   `[data-qa=…]` — added for automation, rarely change.
2. **Semantic attributes / microdata:** `[itemprop=price]`, `[role=…]`,
   `[aria-label=…]`, `[name=…]`, and semantic elements (`article`, `nav`, `time`).
3. **Meaningful, human-named classes:** `.product-card`, `.price`, `.byline` —
   names that describe content, not styling.

Avoid — these break on any redesign:
- **Hashed / generated classes:** `.css-1a2b3c`, `.sc-bdVaJa`. Match the stable part:
  `[class*="price"]`.
- **Utility classes:** `.mt-4`, `.flex` (Tailwind & co.) — layout, not content.
- **Deep positional chains:** `div > div:nth-child(3) > span` — one inserted `<div>`
  and it's wrong.
- **Tag-only selectors:** `span`, `a` — too broad.

Techniques:
- **Anchor on a stable ancestor, then a semantic leaf:** `.product-card .price`.
- **Attribute *contains* for partial-stable classes:** `[class*="teaser"]`,
  `[href*="/product/"]`.
- **XPath when you must match on text:** `//button[normalize-space()="Add to cart"]`.
- **Verify breadth:** a `select_all` should match exactly the set you mean.

## Examples

All queries start at `wq.doc` (the page) — no fetch, no resolve.

Rows → list of dicts:
```python
(
    wq.doc.select_all(".product")
    .extract(
        name=wq.doc.select(".name").text_content,
        url=wq.doc.select("a").attr("href"),
    )
    .project()
)
```

A structured (nested) field — a `price` object via a sub-`extract` + `regex`:
```python
(
    wq.doc.select_all(".product")
    .extract(
        name=wq.doc.select(".name").text_content,
        price=wq.doc.select(".price").extract(
            value=wq.doc.regex(r"[\d.]+"),
            unit=wq.doc.regex(r"[\d.]+\s*(\S+)", group=1),
        ).project(),
    )
    .project()
)
```

Filter — keep in-stock rows (reference an extracted column with `wq.field`):
```python
(
    wq.doc.select_all(".product")
    .extract(
        name=wq.doc.select(".name").text_content,
        in_stock=wq.doc.select(".status").text_content == "In stock",
    )
    .filter(wq.field("in_stock"))
    .project()
)
```

Filter — keep rows WITHOUT a `.sold-out` badge (optional select + `~` + `is_ok`):
```python
(
    wq.doc.select_all(".product")
    .filter(~wq.doc.select(".sold-out", optional=True).is_ok())
    .project()
)
```

Follow a link you extracted (`wq.reference` follows a column holding a link — the one
place `.resolve()` is used):
```python
(
    wq.doc.select_all(".product")
    .extract(link=wq.doc.select("a").attr("href"))
    .extract(detail=wq.doc.reference("link").resolve().select("h1").text_content)
    .project()
)
```

A JSON / API document — the same DSL over dotted paths (`skeleton` shows them):
```python
(
    wq.doc.select_all("results")
    .extract(
        name=wq.doc.select("name").text_content,
        price=wq.doc.select("price").text_content,
    )
    .project()
)
```

Serialise / inspect a plan:
```python
plan.to_blob()    # -> a compact JSON string, portable and safe to store or send
plan.explain()    # -> "Document.select_all('.product').extract(...).project()"
```
