# web.onboard -- Locate + Author

`onboard` splits into two composable, independently-usable phases with typed value models:

```
locate(goal, *, resolver, ...)         -> Reference   # WHERE the dataset is (deterministic)
author(reference, brief, *, resolver, llm) -> wq query   # HOW to extract it (a web.dsl chain)
onboard = author ∘ locate                             # the thin composition on top
```

**Locate** is deterministic (an LLM plugs in only as the optional `search` seed callable).
**Author** is LLM-driven: the model writes the `wq` query from the patterns guide + the page's
hardcoded signals/flags. Both are small units of pure functions + pydantic value models.

## Value models (`models.py`)

- `LocateBrief` -- the *locate*-brief: `goal` (what dataset to find) plus optional `seeds` /
  `candidates` (skip search / skip crawl) and knobs.
- `DatasetBrief` -- the *dataset*-brief: `fields` wanted, optional explicit `selectors`
  (name -> css/json-path override), `download` (want the file, not rows).
- `Reference` -- Locate's output / Author's input: the `url` to query (the **XHR/data-API
  endpoint when one backs the page**, else the page), its `kind`, the `page_url` it was found on,
  the `flags`/`signals` that fired, a suggested `record_selector`, `pagination` remedy,
  `needs_browser`, and the `api_endpoint`.

## Locate (`locate.py`)

`search? -> crawl? -> evaluate every candidate -> select best -> prefer XHR`.

- Seeds come from `brief.seeds`, else an injected `Search` (LLM/heuristic); candidates from
  `brief.candidates`, else a `web.crawl` walk.
- Every candidate is scored with the **whole detection surface**: `web.resolve.flags` (the
  noisy-OR conclusions built from `web.resolve.signals`) + `web.parse` record detection. The
  score is deterministic: dataset-present (a record region / structured-data / a JSON doc) x
  scrapability, minus login/empty/anti-bot penalties; a docs page is banned; a seeded JSON/feed
  doc is forced in (it *is* the dataset).
- **XHR-preference rule**: if a same-origin JSON data-API backing the chosen page is present
  (a captured `NetworkEvent`, a `<link rel=alternate application/json>`, a JSON island, or a
  `/api/…`-looking link) **and its data is consistent with the page** (its JSON leaf values
  overlap the page's record text), the Reference is rooted at *that endpoint* (JSON is cleaner
  than scraping HTML). Consistency is checked by actually resolving the endpoint -- never a
  blind or hallucinated pick.

## Author (`author.py`) -- LLM-driven over natural-language patterns

Author resolves the reference once to get a **sample**, gathers the page's hardcoded
**Signals/Flags** (`web.resolve.flags`), and hands the model two things: (a) those signals/flags,
and (b) the **patterns guide** -- natural-language KNOWLEDGE, not Python classes. The model writes
the `wq` chain; a safe compiler rebuilds it and roots it at the source. Flag-keyed **Behaviours**
then add advisory notes.

- **Patterns are markdown, not code** (`patterns.md` + `patterns.py`). `patterns.md` is the
  query-writing guide: the three-move recipe, how to read leaves/attributes, the transforms, durable
  selectors, and **worked, verified examples** for the well-known structures -- a flat HTML list, an
  HTML table, a JSON/API document (dotted paths + `.attr(key)`), an RSS/XML feed, a list-valued
  field, a class-token value, filtering, a detail-page resolve, and a grouped two-section selector.
  `patterns.py` loads that markdown as `PATTERNS_GUIDE` and `author_prompt(...)` renders it into the
  prompt with the fired signals/flags and a token-lean page skeleton. Adding or refining a "pattern"
  is now an edit to prose + an example, not a new `Pattern` subclass.
- **Only Signals/Flags stay hardcoded** -- they are ground truth about the page (a login wall, a
  pager, a JSON data-API, a record region), computed by `web.resolve`. They steer the prompt (e.g. a
  `paginated` page tells the model to write one page; a `json` kind steers it to dotted paths).
- **The safe compiler** (`compile.py`). `parse_query` rebuilds the model's `wq.doc…` chain by
  walking its AST and driving the REAL `wq` recorder -- attribute access + method calls, the query
  operators (`& | ~`, comparisons) and literal constants only. It is NOT `eval`: only `wq` is a
  name, `_`-prefixed attributes / starred args / any other construct are refused, so a
  prompt-injected line reaching `__globals__` on an untrusted crawled page cannot execute. `reroot`
  prepends `reference(url).resolve()` (the guide has the model write a page-relative chain; the
  pipeline supplies the source), composing the two recordings through the DSL's public plan API into
  one self-contained, portable blob.
- **Behaviours registry** (`behaviours.py`) -- a flag/signal -> a query modifier and/or an advisory
  note the model can't express in a static plan (`paginated`/`infinite_scroll` -> use a paginating
  resolver; `consent_wall`/`tabbed`/`iframe` -> a browser interaction is needed).
- **Deterministic shortcuts** stay code, not model calls: a `download` brief on an HTML page yields
  a file-links query; a binary reference (a PDF/spreadsheet) yields a plain fetch.

## Why this shape

The old monolith (`webclient/pipelines/onboarding/`) folded locate + author + an LLM query-writer
into one 4k-line flow, driven by exactly this idea -- a `lazy_query_guide()` skill (markdown +
examples) rendered into a `write_query` prompt, and a safe AST allowlist (`query_build.py`) that
rebuilt the model's chain without `eval`. This package distils that: Locate stays deterministic
(candidate tiering + scrapability scoring + the XHR-only-when-it-backs-the-page rule); Author keeps
the guide-as-knowledge + safe-compile approach but adapts every example to the NEW `wq` surface and
drops the hardcoded `Pattern`-class registry that had briefly replaced it -- structure-to-query
mapping reads and evolves far better as prose than as an if-chain of classes.

## DSL note

Author emits `wq` chains and needs nothing added to the DSL: HTML and JSON extract with the SAME
verbs -- `wq.doc.select_all(row).extract(field=wq.doc.select(sel).attr("text"))` -- where
`select`/`select_all` take a CSS selector for markup and a dotted JSON path for a JSON document.
The DSL has no `.regex()`, `optional=`, `.as_json()` or `.table()`; the guide uses the leaf reads
(`attr`) and transforms (`number`/`date`/`split`/`map`) instead, and a `select` miss yields null
(never dropping a record), so filtering is explicit. `author` returns the lazy query
(`await q.acollect(resolver=rs)`); `authored` returns its `to_blob()` (rerun with
`web.dsl.run_blob`).
