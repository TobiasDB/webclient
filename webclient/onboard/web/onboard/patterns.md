# Writing a `wq` extraction query

You are given a **page** (its structure, shown as a skeleton) and a **brief** (the fields the
dataset should carry). Write ONE `wq` query that extracts the dataset. Reply with the query
expression ONLY — a `wq.doc…` chain, exactly as written here, no prose and no code fence.

A query is built in three moves:

1. **Pick the repeating record** with `.select_all("<row selector>")` — one match per row of the
   dataset. This is REQUIRED; without it the query extracts nothing.
2. **Pull each field** with `.extract(col=…, …)`; each column is a `wq.doc.select(…)` INTO that row.
3. That is it — the query yields a list of row dicts. (`.project()` is implied; you may add it, but
   the four terminals `collect` / `acollect` / `to_blob` / `run_blob` already project.)

Inside `extract(...)`, `wq.doc` is the **current record**; at the top of the chain it is the
**whole page**. Root every chain at `wq.doc` — the pipeline supplies and fetches the source, so do
NOT write a leading `wq.reference(url).resolve()`.

## Reading a leaf

- `.attr("text")` — an element's text.
- `.attr("href")` / `.attr("src")` — a link (a resolvable reference; call `.resolve()` to follow it).
- `.attr("<name>")` — any other HTML attribute (`data-sku`, `datetime`, `content`, `value`, `alt`, …).
- On a **JSON** document, `.attr("<key>")` reads that key, and `.attr("text")` reads the scalar
  value at the current node.

If the visible text is NOT the value (a star widget, a formatted-vs-machine date, an icon), the
value is almost always in an attribute — read the attribute whose name matches the meaning:

| the value is… | read it with |
| --- | --- |
| a rating / code / flag in a `data-*` attr | `.attr("data-rating")` |
| a machine date | `.attr("datetime")` |
| a link / URL | `.attr("href")` (or `src`) |
| a hidden / meta value | `.attr("content")` |
| an image alt / aria label | `.attr("alt")` / `.attr("aria-label")` |

## Transforms on a leaf

Chain these after a leaf read to shape the value: `.number()` (first number in the text — `"£51.77"`
→ `51.77`, `"22 in stock"` → `22`, and number words — `"Three"` → `3`), `.date()` /
`.datetime()` (any readable date → ISO `YYYY-MM-DD`), `.split(sep)` (text → a list),
`.map({...})` (look a value up in a table), `.link()` (a URL written as text → absolute),
`.regex(pattern, group=1)` (pull a substring out of the text — `"Only $19.99!"` →
`.regex(r"\$([\d.]+)", group=1)` → `"19.99"`).

There is no `.as_json()`; on a JSON document, `.at("dotted.path")` reads a scalar leaf directly.
For a whole `<table>` whose cells are hard to select positionally, a document-level
`.tables("table.x")` returns the rows keyed by header (and `.tables("table.x", transpose=True)`
reads a matrix whose records are COLUMNS).

## `select` is loud — mark optional fields

A `select(css)` that matches NOTHING **raises** (`dsl.select_miss`, naming the selector). That is a
feature: on a required field it tells you the selector is wrong instead of silently dropping data.
For a field that is genuinely absent on some records, opt out per read with
**`select(css, optional=True)`** — a miss then yields null (and the record is kept). Use it for any
optional column and inside a presence test (see filtering, below). `select_all` never raises (it
returns an empty collection), and `.attr(...)` on a null short-circuits to null.

## Durable selectors

Prefer hooks that say *what* a node is over *where* it sits: an id / `[data-testid]`, a semantic
attribute (`[itemprop=…]`, `article`, `time`), or a meaningful class (`.product-card`, `.price`).
Avoid hashed/utility classes and deep positional chains (`div > div:nth-child(3)`). Anchor on a
stable container then a semantic leaf: `.card .price`. Read fields RELATIVE to each record — a page
has chrome (nav / sidebar / footer) that reuses the same class names, so selecting a field at the
top level would pick up the chrome; selecting it inside the record does not.

## Worked examples

### 1 — a flat HTML list

```
main
  div.card
    h2.title  'Aeropress'
    a.link  'view'   (href)
    span.price  '$39'
```
Fields: title, price, url

```python
wq.doc.select_all("div.card").extract(
    title=wq.doc.select("h2.title").attr("text"),
    price=wq.doc.select("span.price").attr("text").number(),
    url=wq.doc.select("a.link").attr("href"),
)
```

### 2 — an HTML table (one record per body row)

The header cells name the columns; each `<tbody>` row is a record. Select the body rows and read
each cell by position with `td:nth-of-type(k)`.

```
table
  thead
    tr
      th 'Item'  th 'Price'  th 'Stock'
  tbody
    tr
      td 'Aeropress'  td '$39'  td '12'
```
Fields: item, price, stock

```python
wq.doc.select_all("tbody tr").extract(
    item=wq.doc.select("td:nth-of-type(1)").attr("text"),
    price=wq.doc.select("td:nth-of-type(2)").attr("text"),
    stock=wq.doc.select("td:nth-of-type(3)").attr("text").number(),
)
```

For a MESSY table — merged cells (`rowspan`/`colspan`), or a transposed matrix whose records are
columns — positional `td:nth-of-type` mis-reads it. Use the document-level `.tables(...)`, which
keys each row by its header (read cells with `.at("<Header>")`):

```python
wq.doc.tables("table.compare", transpose=True)   # records are COLUMNS; each keyed by the first column
```

### 3 — a JSON / API document (dotted paths + `.attr(key)`)

JSON is NOT HTML. `select` / `select_all` take a **dotted path** (`data.items`, `results`), and you
read a record's fields with **`.attr("<key>")`** (there is no `.attr("text")` for a JSON key). A
nested object is a `select` into it, then `.attr` its keys — or a single dotted path to the leaf.

```
{ data: { items: [ { id, name, price: { value, currency } } ] } }
```
Fields: name, price (the numeric value), currency

```python
wq.doc.select_all("data.items").extract(
    name=wq.doc.attr("name"),
    price=wq.doc.select("price.value").attr("text").number(),
    currency=wq.doc.select("price").attr("currency"),
)
```

### 4 — an RSS / XML feed (parsed like HTML)

In RSS the records are `<item>`s and the fields are child tags. Prefer `<guid>` over `<link>` for
the URL when present (many feeds ship an empty or malformed `<link>`).

```
rss > channel > item > (title, link, pubDate)
```
Fields: title, date, url

```python
wq.doc.select_all("item").extract(
    title=wq.doc.select("title").attr("text"),
    date=wq.doc.select("pubDate").attr("text").date(),
    url=wq.doc.select("link").attr("text"),
)
```

### 5 — a list-valued field (use `select_all` inside `extract`)

When a field is itself a LIST within one record (all the tags / images / links), use `select_all`
for that column — it projects to a JSON list of values.

```
div.quote
  span.text  '…'
  small.author  'Albert Einstein'
  div.tags
    a.tag 'change'  a.tag 'thinking'  a.tag 'world'
```
Fields: text, author, tags (a list)

```python
wq.doc.select_all("div.quote").extract(
    text=wq.doc.select("span.text").attr("text"),
    author=wq.doc.select("small.author").attr("text"),
    tags=wq.doc.select_all("a.tag").attr("text"),
)
```

### 6 — a value carried in a CLASS token

A rating coded as an extra class (`class="star-rating Three"`) is not in the text. Read the class
attribute and let the reader pick the token out; the brief will say which token means what.

```
article.product_pod
  p.star-rating.Three
  h3 > a 'A Light in the Attic'
  p.price_color '£51.77'
```
Fields: title, rating (the word after `star-rating`), price

```python
wq.doc.select_all("article.product_pod").extract(
    title=wq.doc.select("h3 a").attr("text"),
    rating=wq.doc.select("p.star-rating").attr("class"),
    price=wq.doc.select("p.price_color").attr("text").number(),
)
```

### 7 — filtering out rows

Drop rows by a predicate with `.filter(...)`. Compare with symbols (`==` `!=` `<` `>`), combine
with `&` `|` `~`. Test presence with `~ <read>.is_ok()`, and mark the tested select
**`optional=True`** — the badge is absent on the rows you want to keep, and a loud (non-optional)
select would raise on them. Read a leaf (`.attr("text")`) so `.is_ok()` sees the value; the leading
`~` turns a missing badge into "keep". Read a column already extracted in the row with
`wq.field("<col>")`.

```python
wq.doc.select_all(".product").filter(
    ~wq.doc.select(".sold-out", optional=True).attr("text").is_ok()   # keep rows with NO sold-out badge
).extract(
    name=wq.doc.select(".name").attr("text"),
)
```

### 8 — fields on the DETAIL page (follow a link, then read)

When a field is not on the listing — only on each item's own page — follow the record's link with
`.attr("href").resolve()`, then select on that page. Never `.resolve()` a text value; only a
reference (an `href` / `src`) resolves.

For SEVERAL detail-page fields, write ONE column per field, each following the SAME link. Repeating
`select("a.detail").attr("href").resolve()` is correct and cheap: within a run the executor fetches
each record's page **once** and reuses it for every column (resolves are memoised by URL), so you do
NOT need — and cannot — bind one resolved page to many fields.

```python
wq.doc.select_all("li.product").extract(
    name=wq.doc.select("a.detail").attr("text"),                                           # on the listing
    sku=wq.doc.select("a.detail").attr("href").resolve().select("[class*=sku]").attr("text"),   # detail page
    price=wq.doc.select("a.detail").attr("href").resolve().select(".price").attr("text"),       # same page, one fetch
)
```
If the detail page is JSON, read it the JSON way (§3): `…resolve().select("stock.count").attr("text")`.

### 9 — two sections, one dataset (a grouped selector)

When the dataset is split across sections that share a record shape (an "Upcoming" list and a
"Past" list), select both with a grouped (comma) selector — it matches all of them in document
order. Mark a section-only field by giving it its own selector; a record that lacks it comes back
null.

```python
wq.doc.select_all("div.callout, li.past").extract(
    title=wq.doc.select("span.what").attr("text"),
    date=wq.doc.select("span.when, span.date").attr("text"),
)
```
