# Case study: making `doc.summary()` / extraction genuinely top-of-class

*Subject:* `/home/zeus/git/web-client` @ `refactor/async-core-backends`
*Scope:* The page **summary / extraction** feature — `doc.summary()` → `Summary` (facet
backings: transport / metadata / structure / runtime / probe) and
`render("markdown"|"text"|"elements"|"links"|"html")`. Optimising for usefulness to
LLMs/agents and to end users.
*Method:* Full read of `webclient/core/document/summary.py`, `.../models.py`,
`.../html.py`, `.../json.py`, `webclient/collection.py` (`extract`/`project`),
`tests/test_summary.py`, `tests/test_render.py`, `demo.py`; cross-read of
`docs/assessment.md` §2.2 and `docs/llm-usability.md` §5–6. Comparison against
Firecrawl, Tavily, Trafilatura/Readability, unstructured.io, Newspaper/jusText,
and JSON-LD/OpenGraph tooling (sources at end). **No code changed.**

---

## 1. State of the art — how leading tools produce LLM-ready page views

The 2025–2026 bar for "URL → LLM-ready" splits into four concrete moves. Leading tools
do all four; most of the value is in the last two.

**(a) Clean main-content → markdown.** The table-stakes move: strip nav/aside/footer/ad
chrome and emit readable markdown. Firecrawl's `/scrape` returns "LLM-ready markdown"
as its headline output and handles JS rendering, proxies and caching behind it. Tavily's
`/extract` "transforms any webpage into LLM-ready data … clean and readable, can be
directly passed to LLMs," offering summary / markdown / cleaned-text formats. The
*content itself* is the product — the readable body text is returned first, not a map of
the page.

**(b) Principled main-content extraction (readability).** Under the markdown sits a
content-vs-boilerplate algorithm. Mozilla **Readability** (Firefox Reader View) and
**Trafilatura** are the reference implementations; the SIGIR-2025 multilingual benchmark
puts both near the top (Trafilatura mean F1 ≈ 0.937 with precision ≈ 0.978; Readability
highest median ≈ 0.970 and most predictable), Trafilatura best on English, Readability
best across most other languages. These use text-density / link-density / DOM-depth
scoring — *not* a hard-coded `main, article` selector. **Newspaper3k** and **jusText**
add article-specific heuristics (byline, publish date, per-block stopword density).

**(c) Element / block segmentation.** **unstructured.io** `partition_html` breaks a page
into typed elements — `Title`, `NarrativeText`, `ListItem`, `Table`, `Image` — each with
metadata; **crucially, `Table` elements carry a `text_as_html` field** so the tabular
structure survives into RAG/LLM pipelines instead of collapsing to a text blob. This is
the shape RAG chunkers consume.

**(d) Schema-guided / structured extraction — the real differentiator.** Firecrawl's
`json`/`extract` format takes a **JSON Schema (or a zod schema, or just a prompt)** and
returns data in *that exact shape* — "define schemas once and get consistent JSON across
any website," handling nested objects and arrays. Two flavours in the wild:
  - **Deterministic**: parse JSON-LD / schema.org / OpenGraph / microdata straight out of
    the page and reshape to the requested keys. JSON-LD (`<script type="application/ld+json">`)
    already encodes `Product` price, `Article` author/date, `Recipe`, `FAQPage`,
    `BreadcrumbList` as machine-readable objects — the highest-signal, zero-inference,
    token-lean structured data on most modern pages.
  - **Semantic**: run an LLM extraction pass server-side against the schema when the data
    isn't marked up. Firecrawl and Tavily both do this; it "understands content
    semantically, not HTML structure."

**Token economics** thread through all four: return markdown/rows not HTML; strip
boilerplate; cap length; offer a `response_format`/verbosity control; paginate large
result sets. The winning output is *small, structured, and directly answerable*.

---

## 2. Our current implementation — honest assessment

### 2.1 What exists and is genuinely good

- **The facet decomposition is a real asset.** `Summary` is five optional sections
  (`models.py:162`), each a pure, deterministic projection of an already-resolved
  document (`summary.py:1`) — no fetch, no escalation. `summary(*include, exclude=...)`
  (`summary.py:325`) lets a caller take exactly the facets it wants. This is a *cleaner*
  contract than Firecrawl's flat blob: an agent can ask for `transport` + `metadata` only
  and pay no tokens for structure.
- **"Keys, not values" is a deliberate, smart token stance for the map facets.**
  Transport reports `header_keys` / `set_cookie_keys` as sorted name lists, values only
  for the few fields that *are* the summary (`summary.py:74`); this keeps the overview
  tiny. Metadata reports `og_keys` and JSON-LD `@type` names (`summary.py:135,145`).
- **Transport / metadata / structure are dense and useful.** CDN/framework detection
  (`_cdn` `summary.py:48`, `_FRAMEWORKS` `summary.py:35`), canonical/feed resolution,
  TOC from headings, word-count + reading-time, internal/external link split, form
  field-name lists, pagination detection (`summary.py:173-221`). Good page *cartography*.
- **The renderer set is the right primitive set** — `render("markdown"|"text"|"elements"|"links"|"html")`
  (`html.py:210`), with `text` doing noise-stripping (`_NOISE` `html.py:20`) and an
  optional `main_content_only` (`html.py:222,226`). `elements` sections blocks under
  headings via `parent_id` (`html.py:100`), the unstructured.io shape.
- **`project(model)` already lands schema *validation*.** Contrary to a stale note in
  `docs/assessment.md` §2.2 (P1-6 "unimplemented"), `collection.py:234` *does* accept a
  pydantic model and validates each extracted row into it — typed, schema-guided rows,
  eager-only. (What it does **not** do is *infer* the extraction — see gaps.)
- **Extensibility is first-class.** A new facet is a `Backing` with `provides`/`gate`;
  a new render format is a branch in `HtmlBacking.render` or an override backing
  (`tests/test_render.py:96` proves `wc.use(Upper())` overrides one format and `super()`s
  the rest). This is the seam every recommendation below rides.

### 2.2 The gaps (the honest part)

1. **No body content in the summary — the biggest LLM miss.** `Summary` is *all map,
   no territory*. An agent handed a `Summary` learns the page has 3 headings, 2 forms and
   is on Cloudflare — but **cannot answer "what does this page say."** Firecrawl/Tavily
   lead with the readable content; ours puts it behind a *separate* `render("markdown")`
   call the agent must know to make. For the single most common agent task ("read this
   page and tell me X"), the summary is the wrong object and the right object is
   undiscoverable from it.
2. **Markdown is lossy on exactly the high-value structures.** `_md_blocks`
   (`html.py:69`) handles headings / `p` / one-level `ul`/`ol` / `pre` / `blockquote` /
   `img` — and **drops tables entirely** (no `<table>` branch; a table's text is lost or
   flattened), **collapses nested lists** (`child.findall("li")` takes only direct `li`
   children and `_inline` flattens any nested `ul`), and ignores definition lists and code
   languages (` ``` ` with no lang, `html.py:86`). Tables are the highest-signal thing an
   LLM wants out of a page; we lose them. (`docs/assessment.md` §2.2 flags this; the
   comparison table there rates us "no tables/nested" vs Firecrawl "best-in-class".)
3. **"Main content" is a hard-coded selector, not readability.** `_main_container`
   (`html.py:95`) is `root.cssselect("main, article, [role=main], #content, #main")` →
   first hit or whole root; `structure.main_content_present` is the same boolean
   (`summary.py:211`). No text-density / link-density scoring. On the (many) pages without
   those tags, `main_content_only=True` silently returns the whole noisy page. This is a
   Newspaper3k/Trafilatura-shaped hole.
4. **JSON-LD and OG *values* are discarded — a self-inflicted wound.** `_ld_types`
   (`summary.py:97`) parses every JSON-LD block and then **keeps only the `@type`
   strings**, throwing away the `Product`/`Article`/`Recipe`/`FAQ` payloads — the richest,
   most deterministic, most token-lean structured data on the page. Same for OG: `og_keys`
   are kept, values dropped (`summary.py:145`). We do the expensive parse and discard the
   prize. A schema-guided extractor would *reconstruct* exactly this.
5. **No schema-*guided* extraction, only schema-*validated*.** `extract(**exprs)` +
   `project(Model)` require the caller to hand-author a selector per field
   (`collection.py:208,234`; `demo.py:290`). There is no `extract(Model)` that *infers*
   the mapping — neither the deterministic path (schema → JSON-LD/OG/microdata) nor the
   semantic path (schema + LLM pass). This is the Firecrawl `json` differentiator, absent.
6. **`elements` has no `table` type.** `_html_elements` (`html.py:100`) emits
   title/text/list_item/code/image — no table, so the unstructured.io `text_as_html`
   trick (structure-preserving tables for RAG) is unavailable.
7. **runtime / probe are empty on a static fetch — correct, but unsignalled.** By design
   `RuntimeBacking.applies` needs browser events (`summary.py:241`) and `ProbeBacking`
   needs a recorded probe (`summary.py:295`), so both are `None` on a plain fetch
   (`tests/test_summary.py:88`). Right behaviour, but an LLM sees a `null` and can't tell
   "not applicable" from "not gathered / render with a browser to populate."
8. **No token budget / verbosity control anywhere.** Neither `summary()` nor `render()`
   takes `max_chars`, a continuation cursor, or a `response_format: concise|full`. A big
   `render("markdown")` can blow the context window with no guard; `link_sample` is capped
   at 10 (`summary.py:216`) but body renders are unbounded.

---

## 3. Best-in-class target — what "fully implemented" looks like here

Prioritised, and sharply split into **LLM-value (differentiators)** vs **table-stakes
(parity)**. The ranking is by LLM value per unit of build effort on *this* architecture.

### Tier A — LLM-value differentiators (build these to be top-of-class)

**A1. A `content` facet: put the body in the summary.** Add a sixth facet whose value is
the main-content excerpt as markdown plus a lead/abstract and a few salients (first N
words, key paragraphs). Then `doc.summary()` is *both* map (facets) and territory
(content) in one token-lean, deterministic object — a strictly better contract than
Firecrawl's flat blob because the caller still selects facets. This is the single highest
LLM-value change: it makes the summary answer "what does this page say" directly.
*This is our differentiator precisely because we already have the facet-composition
machinery no competitor has.*

**A2. Deterministic schema extraction from JSON-LD / OG / microdata.** Stop discarding
the values (gap #4). A `schema` facet (or enrich `metadata`) that surfaces the *pruned*
JSON-LD objects and OG values means an agent asking "price? author? published date?" gets
a deterministic, zero-hallucination, ~free answer on the ~60%+ of modern pages that mark
up. This is high value *and* cheap — the parse already happens.

**A3. `extract(Model)` — schema-guided extraction, one call.** The Firecrawl `json`
differentiator. Signature: `doc.extract(MyModel, prompt=...)` → validated `MyModel`. Two
layers, in order:
  1. **Deterministic first**: try to satisfy the model's fields from JSON-LD/OG/microdata
     (A2) and from field-name↔selector heuristics — no LLM, no cost, fully replayable.
  2. **Semantic fallback**: when fields are unmet, run an LLM pass over the
     `render("markdown", main_content_only=True)` output against the model's JSON Schema.
This is the headline "give me `{title, price}` and I don't care how" capability.
`project(Model)` (already shipped) is the *list* analogue; `extract(Model)` is the
*single-object* one and the natural home for inference.

**A4. Structure-preserving table extraction.** A `render("tables")` mode returning typed
`Table` models (headers + rows) and/or a `table` element type carrying `text_as_html`
(the unstructured.io move, gap #6), plus real markdown tables in `_md_blocks` (see B1).
Tables are the highest-signal LLM payload; preserving them is a differentiator, not
parity, because most "markdown" converters mangle them.

### Tier B — table-stakes (must match to be credible)

**B1. Richer markdown**: tables, recursively-nested lists, definition lists, fenced code
with language. Directly closes the `docs/assessment.md` §2.2 gap and lifts *every*
`render("markdown")` / crawl page an LLM reads.

**B2. Real readability**: a text-density/link-density main-content scorer behind
`_main_container`, falling back to scoring when `main/article` is absent — so
`main_content_only` and `structure.main_content_present` mean something on unmarked pages.
Trafilatura/Readability are the reference algorithms; a lightweight port is enough.

**B3. Token controls**: `max_chars` + continuation cursor and `response_format:
concise|full` on `render()` and `summary()`. Parity with Anthropic tool-efficiency and
Firecrawl length controls.

**B4. Applicability signalling**: distinguish `null` "not applicable" from "not gathered"
on `runtime`/`probe` (e.g. a tiny `available/applies` marker or a docstring the tool layer
surfaces) so an agent knows whether re-rendering with a browser would populate them.

### What stays as-is (already right)

The facet decomposition, "keys-not-values" for the *map* facets, the backing/render
extension seam, `project(Model)` validation, and the deterministic no-escalation stance.
Don't rewrite these — *compose onto* them.

---

## 4. Concrete recommendations for THIS codebase

Keeping cores + backings. The two natural extension points are **a new facet backing**
and **a new render mode / render enrichment** — re-wiring is fine, big rewrites are not.
Each item names its file/seam.

| # | Change | Seam / file | Effort | LLM value |
|---|--------|-------------|--------|-----------|
| R1 | **`content` facet** — main-content markdown excerpt + lead paragraph in the Summary | New `ContentBacking(Backing)` in `webclient/core/document/summary.py`; add `Content` model + `"content"` to `FACETS`/`Summary` in `.../models.py`; reuse `html._md_blocks` + `_main_container` | M | **Highest** |
| R2 | **Capture JSON-LD/OG values** — a `schema` facet (or extend `Metadata`) carrying pruned JSON-LD objects + OG values | `_ld_types`→`_ld_objects` in `summary.py:97`; new `Schema` model in `models.py`; new/extended backing in `summary.py` | S–M | High (near-free) |
| R3 | **`extract(Model, prompt=...)`** single-object schema-guided op: deterministic (R2 + name/selector heuristics) then optional LLM fallback over `render("markdown")` | New `ExtractBacking(Backing)` `provides={"extract"}`, `gate="tree"`, e.g. `webclient/core/document/extract.py`; mirrors `HtmlBacking`; regen stubs via `scripts/gen_stubs.py`. Follow the `add-webclient-feature` skill | L | **Highest** (headline) |
| R4 | **Markdown tables + nested lists + dl + code lang** | `_md_blocks` / `_inline` in `webclient/core/document/html.py:69` (add `<table>`, recurse `ul/ol`, `<dl>`, `<pre><code class=language->`) | M | High |
| R5 | **`render("tables")` + `table` element type** with `text_as_html` metadata | `HtmlBacking.render` branch + `_html_elements` in `html.py:100,210`; `Element` already has a `metadata` dict (`models.py:35`) | M | High |
| R6 | **Readability scorer** behind `_main_container` (text/link-density fallback when `main/article` absent) | `_main_container` / new `_readability()` in `html.py:95`; also feeds `structure.main_content_present` (`summary.py:211`) | M–L | Medium (parity) |
| R7 | **Token controls** — `max_chars`/cursor + `response_format` on `render()`/`summary()` | `render` signature in `html.py:210`; `summary()` in `summary.py:325` | S | Medium (parity) |
| R8 | **Applicability marker** on runtime/probe `None` | `RuntimeBacking`/`ProbeBacking.applies` in `summary.py:241,295`; surface in the tool/MCP layer | S | Low–Medium |

**Sequencing.** R2 (cheap, unblocks R3's deterministic path) → R1 (highest value, small)
→ R4 (lifts every reader) → R3 (headline, largest) → R5/R6/R7/R8. R1+R2+R4 alone move the
summary from "page map" to "LLM-ready page view" and are all small-to-medium edits inside
two files (`summary.py`, `html.py`) plus a couple of models — no architectural change.

**Why a backing/render, not a rewrite.** Every item above is either a new `Backing`
(R1/R2/R3 — same shape as `MetadataBacking`/`HtmlBacking`, chosen by `provides`/`gate`,
overridable via `wc.use(...)` per `tests/test_render.py:96`) or a branch in the existing
`render` dispatch (R4/R5/R7). The `Summary` model absorbing a new optional facet is
additive (`models.py:162`). This is exactly the extension the architecture was built for.

---

## 5. Sources

- [Firecrawl — JSON mode / LLM extract (schema-guided structured output)](https://docs.firecrawl.dev/features/llm-extract)
- [Firecrawl — Scrape API tutorial (markdown / JSON / screenshot formats)](https://www.firecrawl.dev/blog/mastering-firecrawl-scrape-endpoint)
- [Firecrawl — how scraping APIs convert HTML to structured JSON](https://www.firecrawl.dev/glossary/web-scraping-apis/how-web-scraping-apis-convert-html-to-json)
- [Firecrawl — scrape a website to markdown for LLMs](https://www.firecrawl.dev/blog/scrape-a-website-to-markdown)
- [Tavily — best practices for `/extract` (LLM-ready page content)](https://docs.tavily.com/documentation/best-practices/best-practices-extract)
- [Tavily — `/search` vs `/extract`, when to use each](https://medium.com/@sofia_51582/tavilys-search-vs-extract-apis-and-when-to-use-each-67cc70edd610)
- [SIGIR 2025 — Multilingual Evaluation of Main Content Extractors (Trafilatura vs Readability)](https://dias.users.greyc.fr/publications/sigir2025.pdf)
- [Trafilatura — benchmarks and evaluation](https://trafilatura.readthedocs.io/en/latest/evaluation.html)
- [Trafilatura vs Readability vs Newspaper4k comparison](https://www.contextractor.com/trafilatura-vs-readability-vs-newspaper/)
- [unstructured.io — document elements (Title/NarrativeText/ListItem/Table)](https://unstructured.readthedocs.io/en/main/introduction/overview.html)
- [unstructured.io — table extraction (`text_as_html` on Table elements)](https://docs.unstructured.io/examplecode/codesamples/apioss/table-extraction-from-pdf)
- [Apify — Structured Data Extractor (JSON-LD / OpenGraph / Microdata)](https://apify.com/gratifying_graph/structured-data-extractor)
- [Anthropic — Writing effective tools for AI agents](https://www.anthropic.com/engineering/writing-tools-for-agents)
- Internal: `docs/assessment.md` §2.2, `docs/llm-usability.md` §5–6.
