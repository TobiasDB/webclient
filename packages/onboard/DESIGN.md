# web.onboard -- Locate + Author

`onboard` splits into two composable, independently-usable phases with typed value models:

```
locate(goal, *, resolver, ...) -> Reference        # WHERE the dataset is
author(reference, brief, *, dsl) -> wq query        # HOW to extract it (a web.dsl chain)
onboard = author ∘ locate                            # the thin composition on top
```

Each phase is a small set of pure functions + pydantic value models + a registry; neither
depends on an LLM (an LLM is an *optional* injected search / field-mapper, not the engine).

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

## Author (`author.py`) -- registry-driven

Author resolves the reference once to get a **sample**, picks the best-matching **Pattern**, and
builds a `web.dsl` (`wq`) chain; **Behaviours** keyed on the reference's flags then modify it.

- **Patterns registry** (`patterns.py`) -- well-known structures -> their query shape. Each is a
  `Pattern` (a structural `Protocol`: `match(reference) -> score`, `build(reference, brief, sample)
  -> a wq collection/document`). Shipped: `json_array` (a data-API envelope -> `.at(path).pluck(...)`),
  `repeating_records` (a list -> `.select_all(row).project(...)`), `html_table` (a header
  `<table>`, column-by-header), `file_download` (a binary reference, or a listing of file links).
  Register another (a key/value spec table, JSON-LD, ...) with `@pattern`.
- **Behaviours registry** (`behaviours.py`) -- a flag/signal -> a query modifier. Shipped:
  `record_list` -> `.nonempty().distinct()`, `paginated`/`infinite_scroll` -> note a paginating
  resolver is needed, `consent_wall`/`tabbed` -> note an interaction is needed. Register another
  with `@behaviour`.
- **Field mapping** is deterministic: a brief field name maps to a record sub-selector by
  class / `itemprop` / `data-*` (HTML) or a matching key (JSON); an explicit `brief.selectors`
  entry always wins.

## Why this shape

The old monolith (`webclient/pipelines/onboarding/`) folded locate + author + an LLM query-writer
into one 4k-line flow. The learnings distilled here: candidate tiering + scrapability scoring
(`select.py`/`evaluate.py`), the XHR-only-when-it-backs-the-page rule (`reference.py`), the
flags-as-ground-truth-for-structure rule (`evaluate.py`), and pattern-shaped queries
(`query_build.py`). What is dropped: the LLM writing raw query *code* (and its AST sandbox) --
here the query shape is chosen by an extensible registry, and only field naming may (optionally)
consult a model.

## DSL note

Author emits `wq` chains and needs nothing added to the DSL: HTML and JSON extract with the SAME
verbs -- `wq.reference(url).resolve().select_all(row).extract(field=wq.doc.select(sel).attr("text"))`
-- where `select`/`select_all` take a CSS selector for markup and a dotted JSON path for a JSON
document. `author` returns the lazy query (`await q.acollect(resolver=rs)` / `.collect()`);
`authored` returns its `to_blob()` (rerun with `web.dsl.run_blob`).
