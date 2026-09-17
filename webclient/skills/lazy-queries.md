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

### Reading a value from an ATTRIBUTE, not the text

Many values live in an **attribute**, not the visible text — and `.attr("text")` returns
nothing for them. When the value you need is not the element's text, read the attribute by
name. Look at the skeleton: the element's real attributes are shown (`<span data-rating="Four">`,
`<time datetime="2026-09-14">`, `<a href="/p/1">`, `<meta content="...">`, `<data value="42">`).

| the value is… | read it with | example element |
| --- | --- | --- |
| a rating / code / flag in a `data-*` attr | `.attr("data-rating")` | `<span class="stars" data-rating="Four"></span>` (text is empty!) |
| a machine date | `.attr("datetime")` | `<time datetime="2026-09-14">Sep 14</time>` |
| a link / URL | `.attr("href")` (or `src`) | `<a href="/p/1">…</a>` |
| a numeric value in an attr | `.attr("value")` / `.attr("data-value")` | `<data value="42">forty-two</data>` |
| a hidden/meta value | `.attr("content")` | `<meta itemprop="price" content="19.99">` |
| an image alt / aria label | `.attr("alt")` / `.attr("aria-label")` | `<img alt="Red mug">` |

Rule of thumb: if the visible text is NOT the value (e.g. a star widget, an icon, a
formatted-vs-machine date), the value is almost always in an attribute — pick the attribute
whose name matches the meaning. Only `.regex()` the **text**; it can't reach an attribute.

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
- **A nested field** (a schema path like `price.value` / `price.unit`, i.e. a branch with
  children) → the column is its own **sub-`extract`** that ends in its own `.project()`:
  `price=wq.doc.select(".price").extract(value=…, unit=…).project()`, so the output JSON nests
  exactly like the schema. Two rules: (1) the sub-extract's leaf selectors are **relative to the
  branch container** you selected (`value`/`unit` are found INSIDE `.price`); (2) every REQUIRED
  leaf must resolve to real content — a branch that comes back `{"value":"","unit":""}` counts as
  empty, so pick a leaf selector/accessor (text, `.regex(...)`, or an `.attr(...)`) that actually
  hits the value. If a leaf lives on the detail page, resolve to it (see the nested-resolve
  example) — a nested branch can itself contain a `.resolve()`.

### When records have no wrapper (flat sibling runs)

Some lists have **no element that wraps each record** — the fields sit side by side as
sibling elements, often split by a separator. A press-release list is the classic case:
a `<p>` with the title link, then a *separate* sibling `<p>` with the date, then an
`<hr>`, repeating:

```
<p><a href="…/adobe-to-acquire-semrush">Adobe to Acquire Semrush</a></p>
<p>November 19, 2025</p>
<hr>
<p><a href="…">next title</a></p>
<p>next date</p>
```

There is no `.news-item` to select, and the date is **outside** the title element, so a
plain `select_all("p")` splits every record in two. Handle it in two moves:

- **Pick the record by its distinguishing child**, with `:has(...)`: the record is the
  paragraph that contains the link → `select_all("p:has(a)")`. (Attribute selectors are
  not allowed *inside* `:has()` — write `:has(a)`, not `:has(a[href])`.)
- **Reach a following-sibling field** with the adjacent-sibling combinator from
  `:scope` — the current record: `date = wq.doc.select(":scope + p").attr("text")`
  reads the very next `<p>`. (Only the `:scope + …` form works; a bare `+ p` is a syntax
  error.) Mark it `optional=True` if it is not always present.

```python
wq.doc.select_all("p:has(a)").extract(
    title=wq.doc.select("a").attr("text"),
    url=wq.doc.select("a").attr("href"),
    date=wq.doc.select(":scope + p", optional=True).attr("text"),
).project()
```

If the run is polluted by other anchored paragraphs (nav, tools, footer), narrow the
record selector to the ones you mean. The cleanest way is an **XPath that matches on the
link target**, since attribute matching is not allowed inside CSS `:has()`:

```python
wq.doc.select_all("//p[a[contains(@href, '/news/')]]").extract(
    title=wq.doc.select("a").attr("text"),
    url=wq.doc.select("a").attr("href"),
    date=wq.doc.select(":scope + p", optional=True).attr("text"),
).project()
```

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

### 3. A JSON / API document (dotted paths + `.attr(key)`)

JSON is NOT HTML: `select`/`select_all` take **dotted paths** (`data.items`, `results[0]`),
and you read a record's fields with **`.attr("<key>")` directly** — there is no `.attr("text")`
on JSON (that is an HTML thing and returns null here). A nested object is a `select` then
`.attr` on its keys.

Skeleton (the shape, from a JSON document):
```
{
  data: {
    results: [12]
      id: number
      name: string
      price: { amount: number, currency: string }
```
Schema:
```
- id — the record id
- name — the record name
- price.amount / price.currency — the structured price
```
Query:
```python
wq.doc.select_all("data.results").extract(
    id=wq.doc.attr("id"),                 # read the key off the record — NOT .select("id").attr("text")
    name=wq.doc.attr("name"),
    price=wq.doc.select("price").extract(  # a nested object: select it, then .attr its keys
        amount=wq.doc.attr("amount"),
        currency=wq.doc.attr("currency"),
    ).project(),
).project()
```

**JSON injected into an HTML page.** When the DOM is a shell but the records are inlined in a
`<script type="application/json">` island (or a `__NEXT_DATA__` / ld+json blob), select that
script and **`.as_json()`** to reparse its text as a JSON document, then use the dotted-path
form above:
```python
wq.doc.select("script#__DATA__").as_json().select_all("catalog.items").extract(
    title=wq.doc.attr("title"),
    sku=wq.doc.attr("sku"),
).project()
```

**An RSS/XML feed** is parsed like HTML. In **RSS** the records are `<item>`s and the fields are
child tags (`.select("title").attr("text")`, `.select("pubDate").attr("text")`, `.select("link")`
or `.select("guid")` for the URL). In **Atom** the records are `<entry>`s, the URL is
`.select("link").attr("href")`, and the date is `<published>` / `<updated>` — so check whether the
feed uses `<item>` (RSS) or `<entry>` (Atom) before choosing the record selector. Tag names are
matched case-insensitively (`pubdate` finds `<pubDate>`), but write them as the feed spells them.

### 4. A field that lives on the DETAIL page (nested resolve)

When a required field is **not on the listing** — it only appears on each item's own page —
follow the item's link and `.resolve()` it, then select on that page. Do NOT guess an
attribute (`data-sku`) that isn't in the record: resolve the link and read the real value.

Skeleton (the listing has a link but no SKU):
```
<li class="product">
  <a class="detail" href="/item/123">Widget Pro</a>   ← SKU is on /item/123, not here
```
Query (resolve the href per record, then select on the detail page):
```python
wq.doc.select_all("li.product").extract(
    name=wq.doc.select("a.detail").attr("text"),
    sku=wq.doc.select("a.detail").attr("href").resolve().select("[class*=sku]").attr("text"),
).project()
```
Chain another `.resolve()` for a field two pages deep (listing → detail → spec page), and
end with `.regex(...)` if the value is buried in prose:
`...attr("href").resolve().select("a.spec").attr("href").resolve().select(".body").regex(r"ID:\s*([A-Z0-9-]+)", group=1)`.

### 5. Filtering — drop rows with a sold-out badge

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
