# Friction log

Rough edges found by *using* the library (dogfooding), then correlated into a
prioritised solution. The broad structure (cores + backings + dispatch) stays;
these are re-wirings and small fixes, not rewrites.

## Observations

| # | Where | What happened | Severity |
|---|---|---|---|
| F1 | `summary()` | The summary is all shape, no substance — it reports transport/metadata/structure but **carries no body text/markdown**, so it can't answer "what does this page say" (the most common agent task). | High |
| F2 | `render("markdown")` | A page with a `<table>` renders to markdown with the **table dropped entirely** (`# Home` only). Firecrawl-grade markdown is the headline "LLM-ready" feature. | High |
| F3 | `render("markdown")` | **Nested lists are broken**: `<li>two<ul><li>nested</li></ul></li>` renders `- two`**`nested`** — child list text is concatenated onto the parent with no separator or indentation (`_inline`/`_md_blocks`, `html.py`). | Medium |
| F4 | `Collection.project()` / `extract` | Extracting an `attr("href")` column keeps a `Reference` in the row (not a URL string). **This is by design** — a later step follows it (`doc.reference("link").resolve()`), and the *service* serialises cores to handles over the wire. Friction only for a naive `json.dumps(rows)` off a local (not service) result. Not a bug; a `project(flatten=True)` / `.urls()` convenience + a doc note would remove the surprise. | Low |
| F5 | `select(".nope")` | A missing selection **raises a bare `LookupError`** by default (you must know to pass `error=RETURN`). It's harsh for an agent and *inconsistent*: fetch failures give a structured `WebException` + a not-ok Document, but a miss gives a builtin exception. | Medium |
| F6 | `search(endpoint=<fails>)` | A search whose results page fails **raises `WebException`** rather than returning `[]` (or a not-ok result). Debatable, but an agent calling `search` expects a list, not an exception. | Low |

## Correlation — two themes

**Theme A — content extraction is weak (F1, F2, F3).** The dominant theme, and it
matches the independent `docs/research/summary.md` finding ("all map, no
territory"). The markdown renderer is lossy (tables, nested lists) and the summary
omits the body. Together these undercut the core "LLM-ready page" value
proposition. (F4 is by design, not part of this theme.)

**Theme B — miss / error ergonomics (F5, F6).** "Raise vs return" is inconsistent
across the surface: fetches are lenient-capable and structured; selects and searches
raise (sometimes builtins). An agent has to learn per-op which is which.

## Proposed solution (prioritised)

**P1 — content (Theme A), highest leverage:**
1. **Fix the markdown renderer** (`html.py` `_md_blocks`/`_inline`): nested lists
   (recurse with indentation) and **tables** (`<table>` → GFM pipe table). Small,
   self-contained, high value; add render tests. *(F2, F3)* — **DONE** (this pass).
2. **A `content` summary facet** — a main-content markdown excerpt so `summary()`
   carries body, not just structure (a new facet backing; `summary.py` + a `Content`
   model). *(F1)* — see `docs/research/summary.md` for the full design (readability,
   token budget, then schema-guided `extract(Model)`).

**P2 — error ergonomics (Theme B):**
4. Make a missing `select` return a **not-ok Document by default** (opt back into
   raising with an explicit policy), OR at minimum raise a structured
   `WebException`/`LookupError` subclass carrying the selector — consistent with the
   fetch path. *(F5)*
5. Decide `search`-on-failure: return `[]` with the failure recorded, vs raise.
   Pick one and make it the documented contract. *(F6)*

P1.1 and P1.2 are safe, small re-wirings with tests (done first). P1.3 and P2 are
small design decisions worth a human call; noted here rather than changed blindly.
