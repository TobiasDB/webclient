---
name: web-queries
description: >-
  How to write a web query that extracts structured data from a page: the query
  syntax, how to write durable CSS selectors, how to turn a target schema into a
  query, and worked skeleton -> schema -> query examples.
---

# Writing a web query

A query extracts structured data from one page. You write it against `wq.doc` — the
page — and it returns a list of rows. Build it in three moves:

1. **Pick the repeating record** with `.select_all("<row selector>")` — one match per
   row of the dataset.
2. **Pull each field** with `.extract(col=…, …)`; each column is a `wq.doc.select(…)`
   into that row.
3. **Finish with `.project()`** to get a list of dict rows.

Inside `extract`, `wq.doc` is the **current row**; at the top of the chain it is the
**whole page**.

## Op reference

<!-- OP-REFERENCE -->

`.attr("text")` gives an element's text; `.attr("href"|"src")` gives a link (has
`.url`); `.attr("data-…")` gives that HTML attribute. `.regex(pattern)` pulls a
substring out of an element's text (`group=1` for the first capture group).

## Syntax rules

- **Conditions use symbols, not words.** In a `filter`, compare with `==` `!=` `<`
  `<=` `>` `>=` and combine with `&` (and) `|` (or) `~` (not), parenthesising each
  side: `(a) & (b)`. Never use `and` / `or` / `not` / `if` / `for` / `len()`.
- **A miss is an error.** `select` / `attr` raise if they match nothing — which is
  what you want for a required field. For a field that is genuinely sometimes absent,
  pass `optional=True` and branch on it with `.is_ok()` / `.is_empty()`.
- **Calls vs the whole chain.** `select(...)`, `attr(...)`, `regex(...)`, `project()`
  take `()`; end every query with `.project()`.

## Turning a schema into a query

You are given a target **schema** — the fields each record should carry, sometimes
nested, each with a short description. Map it mechanically:

- **The row** → `.select_all("<selector for the repeating record>")` (find the
  container that appears once per record in the skeleton).
- **A flat field** → a column `name=wq.doc.select("<selector>").attr("text")`. Use the
  description to pick the selector and the accessor: a link field → `.attr("href")`; a
  code/attribute field → `.attr("data-…")`; plain text → `.attr("text")`.
- **A numeric / split field** → `.regex(...)` on the element's text to pull just the
  number or unit.
- **A nested field** (has children) → the column is a **sub-`extract`**:
  `price=wq.doc.select(".price").extract(value=…, unit=…).project()`, so the output
  JSON nests exactly like the schema.

## Writing durable CSS selectors

A selector is only as good as it is stable — pages get restyled and reordered. Prefer
hooks that say *what* a node is over *where* it sits or how it looks.

Prefer, best first:
1. **Test/id hooks:** `#id`, `[data-testid=…]`, `[data-qa=…]` — rarely change.
2. **Semantic attributes / microdata:** `[itemprop=price]`, `[role=…]`,
   `[aria-label=…]`, and semantic elements (`article`, `nav`, `time`).
3. **Meaningful classes:** `.product-card`, `.price` — names that describe content.

Avoid:
- **Hashed / utility classes:** `.css-1a2b3c`, `.mt-4`, `.flex` — match the stable part
  instead: `[class*="price"]`.
- **Deep positional chains:** `div > div:nth-child(3) > span` — one inserted node and
  it breaks.
- **Tag-only selectors:** `span`, `a` — too broad.

Techniques:
- **Anchor on a stable container, then a semantic leaf:** `.product-card .price`.
- **Attribute *contains* for partly-stable classes:** `[class*="teaser"]`,
  `[href*="/product/"]`.
- **XPath to match on text:** `//button[normalize-space()="Add to cart"]`.
- **Check breadth:** a `select_all` should match exactly the records you mean.

## Worked examples

### 1. A flat listing

Skeleton:
```
<ul class="products">
  <li class="product">
    <span class="name">…</span>
    <span class="price">…</span>
    <a class="detail" href="…">…</a>
```
Schema:
```
- name — the product's display name
- price — the listed price
- url — a link to the product page
```
Query:
```python
wq.doc.select_all("li.product").extract(
    name=wq.doc.select(".name").attr("text"),
    price=wq.doc.select(".price").attr("text"),
    url=wq.doc.select("a.detail").attr("href"),
).project()
```

### 2. A nested field (structured price) with regex

Skeleton:
```
<div class="card" data-sku="…">
  <h3 class="title">…</h3>
  <div class="price">30 $ / 1TB</div>
```
Schema:
```
- name — the product name
- sku — the product code
- price — the price object
  - value — the numeric amount only
  - unit — the currency or unit
  - modifiers — any qualifier (e.g. per 1TB)
```
Query:
```python
wq.doc.select_all(".card").extract(
    name=wq.doc.select(".title").attr("text"),
    sku=wq.doc.select(".card").attr("data-sku"),
    price=wq.doc.select(".price").extract(
        value=wq.doc.regex(r"[\d.]+"),
        unit=wq.doc.regex(r"[\d.]+\s*(\S+)", group=1),
        modifiers=wq.doc.regex(r"/\s*(.+)$", group=1),
    ).project(),
).project()
```

### 3. A JSON / API document (same syntax, dotted paths)

Skeleton:
```
{
  results: [12]
    id: number
    name: string
    inStock: bool
```
Schema:
```
- id — the record id
- name — the record name
```
Query:
```python
wq.doc.select_all("results").extract(
    id=wq.doc.select("id").attr("text"),
    name=wq.doc.select("name").attr("text"),
).project()
```

### 4. Filtering — drop rows with a sold-out badge

Skeleton:
```
<div class="product">
  <span class="name">…</span>
  <span class="sold-out">Sold out</span>   ← only on some rows
```
Query:
```python
wq.doc.select_all(".product").filter(
    ~wq.doc.select(".sold-out", optional=True).is_ok()
).extract(
    name=wq.doc.select(".name").attr("text"),
).project()
```
