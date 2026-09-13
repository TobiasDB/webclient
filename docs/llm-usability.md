# LLM-Usability Assessment: `webclient` as an LLM-drivable web client

*Subject:* `/home/zeus/git/web-client` @ `refactor/async-core-backends`
*Scope:* How usable and effective is this library **for large language models to drive**, across three exposure modes: (1) a query DSL an LLM emits, (2) an MCP server over the HTTP API, (3) a Python-callable tool set.
*Companion:* This builds on `docs/assessment.md` (general engineering assessment). It does **not** repeat that report's findings on streaming/crawl/resilience except where they bear on LLM use. No library code was changed.
*Method:* Full read of `expr.py`, `plan.py`, `executor.py`, `surfaces.py`, `collection.py`, `service.py`, `core/remote_core.py`, `core/document_core.py`, `example.py`, `demo.py`, tests; comparison against the MCP 2025-06-18 spec, Anthropic's "Writing effective tools for AI agents" guidance, and shipped LLM web tools (Firecrawl, Tavily). Citations at the end.

---

## 1. Executive summary

**Verdict: a superb *compile target* and a poor *emission target*.** The library's internal machinery — one recorder, one serializable Plan IR, one executor — is exactly the kind of clean, uniform substrate you want *behind* an LLM tool layer. But nothing here is yet shaped for a model to *drive directly*: there is no tool layer, no task-oriented surface, no structured errors, and the one thing an LLM would have to emit today (raw `Plan` JSON, or Python method chains) is verbose, under-specified, and unsafe to generate blind. The good news is that the distance from "great kernel" to "LLM-native product" is short, because the kernel already gives you server-side execution, a wire format, and markdown/structured rendering for free.

**Ranked recommendation — which mode to invest in first:**

1. **Mode 3 (a small Python-callable tool set) — build this first.** Highest end-user value per unit of effort, lowest LLM failure rate, and it *is* the thing Firecrawl/Tavily ship. Six task-verbs (`fetch`, `extract`, `search`, `crawl`, `render_markdown`, `screenshot`) wrapping the existing surface, returning markdown/rows, hide 100% of the lazy/plan machinery. The primitives already exist (`document_core.py:203` `summary`, `:290` markdown render, `collection.py:190` `extract`/`:211` `project`).
2. **Mode 2 (MCP server) — build this second, as the packaging of Mode 3.** MCP is a distribution channel, not a different design: the right MCP is "expose the Mode-3 tools with JSON Schemas + structured output + resource links," not "expose `POST /execute`." The current `/execute` (`service.py:59`) maps poorly to MCP tools as-is (see §4.2).
3. **Mode 1 (emit the DSL / Plan IR directly) — do this last, and only for a power-user / self-hosted niche.** The Plan IR is a genuinely good *internal* IR and a fine *audit/telemetry* format, but a mediocre thing to ask a model to author from scratch versus SQL/GraphQL/JQ, which have years of training-data priors. If you expose authoring at all, expose the *Python chain* (which is in the training distribution) and compile it server-side — do not ask the model to hand-write `Plan` JSON.

**The single highest-leverage change to make this library "LLM-native":** add a **server-side task-verb layer** (Mode 3) that returns LLM-ready markdown/rows and *builds the plans itself*, so the model never emits plan IR and never sees `_core`/`Expr`/`Collection`. Everything else (MCP wrapper, structured errors, pagination) hangs off that one layer. Second-highest: give errors a **structured, actionable JSON shape** (today they are `HTTPException(detail=str)` — `service.py:57,68,89`) and add a **URL/host safety boundary** (today the only boundary is `_`-refusal — `plan.py:63`; there is no SSRF/allowlist/cost guard at all).

**Cross-mode reality checks that hit all three:**
- **Token economics are already half-solved.** `render("markdown")` / `render("text", main_content_only=True)` / `render("elements")` (`document_core.py:290`, `:301`, `:310`) and `summary()` (`document_core.py:203`) are the correct LLM-ready outputs. What's missing is that they aren't the *default* surface and aren't paginated/truncated.
- **Errors are not agent-recoverable.** A not-ok document carries a serializable `WebError` with `type`/`message`/`status_code` (`errors.py`), which is good — but the *service* flattens everything to `HTTPException(status_code, detail=str)` (`service.py:57,68,72,89,125`), and plan-authoring errors surface as Python `TypeError`/`AttributeError` strings (`expr.py:91`). An autonomous agent cannot branch on those reliably.
- **Safety is thin for arbitrary emission.** `validate_names` (`plan.py:55`) only rejects `_`-names, unknown roots, and unknown operators. An LLM-emitted plan can fetch *any* URL (including `http://169.254.169.254/` or internal hosts — no allowlist anywhere in `webclient/`), drive a real browser, take screenshots, and fan out unboundedly over `select_all` (`executor.py:94`). For Modes 1 and 2 this is a real SSRF/cost surface.

---

## 2. What the LLM actually has to produce (the crux)

Every mode reduces to one question: *what tokens does the model emit, and how likely are they to be correct?* Three candidate emission forms exist in this repo today:

**(a) Python method chains** (`demo.py:244-255`):
```python
reference(url).resolve().select_all(".card").extract(
    title=doc.select(".title").attr("text"),
    price=doc.select(".price").attr("text"),
    link=doc.select("a").attr("href"),
).filter(doc.field("price") != "").project()
```
This is the most learnable form: it reads like Polars/Django-ORM, both heavily represented in training data. But it has three LLM-hostile quirks: the **eager/lazy vocabulary split** (`page.title` eager vs `doc.select(...).attr("text")` lazy — `demo.py:183` vs `:246`), the **module-global roots** `doc`/`ref`/`many` that shadow locals (`expr.py:226-228`), and the **two triggers** `.collect()` vs `wc.execute()` (`expr.py:107`, `surfaces.py:352`) — the model must learn *when* each applies. Each is a documented foot-gun (see `docs/assessment.md` §2.1).

**(b) The Plan IR JSON** (`plan.py`; the wire form the service consumes — `service.py:66`):
```json
{"version":1,"root":"Reference","source":{"scheme":"http","hostname":"...","path":"/"},
 "steps":[{"kind":"call","name":"","args":[],"kwargs":{}}, ...]}
```
This is what `/execute` actually eats. As an LLM target it is **verbose and low-signal**: a two-line Python chain becomes ~40 lines of nested `{kind,name,args,kwargs}` with `get`+`call` step *pairs* for every method (`executor.py:111` shows `get` followed by `call` is the calling convention), sub-plans nested inside `Arg.plan` (`plan.py:16-21`), and operators encoded as `op` steps (`plan.py:37`). No model will author this reliably, and it burns 5-10× the tokens of the Python form for the same query. It is an excellent *compiler output* and a poor *model output*.

**(c) A task-verb call** (does not exist yet, Mode 3): `extract(url, schema={...})` → rows. One line, no IR, no chain vocabulary. This is the Firecrawl/Tavily shape and the one models get right first try.

**Conclusion that drives the whole report:** the Plan IR should stay an internal/telemetry artifact; the LLM-facing emission should be either (b-as-target only for power users who compile from Python) or, far better, (c). This is why Mode 3 ranks first.

---

## 3. Mode 1 — the lazy DSL / Plan IR as an LLM query language

**Rating: WEAK (as a direct emission target) / ADEQUATE (as an internal compile target).**

### What's genuinely good for an LLM
- **A closed, inspectable grammar.** The IR is tiny: 5 step kinds (`plan.py:27`), 6 roots (`plan.py:35`), 9 operators (`plan.py:37`), 2 free functions (`plan.py:39`). A short spec *can* enumerate the entire surface — that is rare and valuable. `describe()` (`plan.py:74`) gives a human/LLM-readable rendering of any plan, which is a good few-shot and self-check aid.
- **Composability is real and uniform.** Sub-expressions nest as `Arg.plan` (`plan.py:20`), so `extract`/`filter`/`when` take arbitrary sub-queries evaluated per element (`executor.py:144`, `collection.py:150`). `when(cond).then(a).otherwise(b)` (`expr.py:181`) is a clean, SQL-`CASE`-like primitive an LLM can pattern-match to.
- **"Illegal states are unrepresentable," loudly.** `bool()`/`len()`/`iter()` on a lazy expr raise a message naming the recordable form (`expr.py:90-104`). If an LLM writes `if doc.select(...):` the error *tells it* to use `& | ~`. That is exactly the kind of self-correcting error message Anthropic's tool guidance calls for — but it only exists for this one class of mistake.
- **The wire form is the audit form.** Because the plan *is* serializable (`plan.py:42`), an agent's every query is inspectable/loggable/replayable JSON. For agent observability and for a human-in-the-loop "approve this plan" gate, that is a strong asset SQL/Playwright don't give you for free.

### What hurts an LLM
- **The IR is a poor authoring target vs. its peers.** LLMs are excellent at SQL, GraphQL, and JQ because those are dense, in-distribution, and have canonical error messages. This IR is out-of-distribution (nobody trained on `{"kind":"get","name":"select_all"}`), verbose, and has a non-obvious calling convention (`get` then `call` steps — `executor.py:111`). Compared to Playwright (imperative, in-distribution) it loses on familiarity; compared to JQ (one-line extraction) it loses on density.
- **The two-tier eager/lazy typing confuses generation.** Inside a plan you must write `.attr("text")`; on a materialized document you write `.text` (`demo.py:246` vs `:183`; the split is baked into `document_core.py`). An LLM cannot tell from context which tier it is in, and the generated stubs (`surfaces.py:127` `text` property vs the lazy attr op) reinforce the ambiguity. This is the single most likely source of malformed queries.
- **Errors during authoring are Python tracebacks, not recoverable signals.** A bad chain raises `AttributeError`/`TypeError` (`expr.py:41-43,91`) or, at the service, a 422 with a stringified `ValueError` (`service.py:67-68`). There is no "did you mean" / no machine-readable error code / no pointer to the offending step. An agent gets a string and must guess.
- **Safety boundary is too coarse for arbitrary emission.** `validate_names` (`plan.py:55`) is a *name* filter, not a *capability* filter. It happily validates a plan that fetches an internal URL, opens a browser (`browser=True` — `surfaces.py:76`), screenshots, or fans out over 10k nodes. There is no URL allowlist, no per-plan op budget, no depth cap anywhere in the tree. For an LLM emitting plans against a hosted service this is an SSRF + cost-DoS surface.

### The eager/lazy question, directly
The two-tier typing **hurts** generation more than it helps. Its benefit (IDE autocomplete + mypy for human authors) is invisible to an LLM, which sees only text. Its cost (`.text` vs `.attr("text")`, `.collect()` vs `wc.execute()`) is paid on every generation. For an LLM target you want *one* vocabulary. If Mode 1 is ever exposed, collapse to the lazy vocabulary only and one trigger.

### Verdict for Mode 1
Keep the Plan IR as the **internal compile target and audit log** — it is excellent at that. Do **not** ask an LLM to emit it. If you want a directly-authored DSL, expose the **Python chain** (in-distribution) and compile it, and first fix the eager/lazy split, the global roots, and the double trigger. Even then it is a power-user feature, behind Mode 3 in priority.

---

## 4. Mode 2 — an MCP server over the HTTP API

**Rating: WEAK today (the current `/execute` shape does not map to good MCP tools) / ADEQUATE-once-wrapped.**

### 4.1 What MCP wants in 2025
Per the MCP 2025-06-18 spec, a good server exposes: **tools with input *and output* JSON Schemas** (structured tool output landed in 2025-06-18), **resources addressed by URI** for readable context, **cursor-based (opaque) pagination** for large result sets, and **structured, actionable errors**. Anthropic's tool-design guidance adds: **few high-signal tools**, **consolidate multi-step workflows into one tool**, **namespaced names**, **token-efficient responses with truncation/`response_format` control**, and **error text that steers the model to the fix**. [refs below]

### 4.2 How the current API maps — and where it breaks
- **`POST /execute {plan|url|document_id}` (`service.py:59-81`) is a *plan interpreter*, not a *tool*.** Exposing it verbatim as one MCP tool called `execute` would force the LLM to emit Plan IR (Mode 1's weakness) as the tool argument — the worst of both worlds. It also fails Anthropic's "one high-signal tool per workflow": a single omnibus `execute(plan)` maximizes the model's degrees of freedom to be wrong.
- **The `_RemoteDoc` handle model is MCP-hostile as a default.** A fetched document returns a handle `{"__doc__":{id,kind,ok,title}}` (`service.py:34`, `remote_core.py:30`), and *every* subsequent op is a separate round-trip rooted at that id (`remote_core.py:37-44`; comment: "one more round trip"). For an agent this means N tool calls to get N fields, each burning a full model turn. MCP's answer is the opposite: return the **content** (markdown/rows) in the *first* call, and only use a handle/resource-URI for genuinely large bodies fetched on demand. The handle model is right for a *programmatic* client and wrong for an *agentic* one.
- **Errors are unstructured.** Everything is `HTTPException(status_code, detail=<string>)` (`service.py:57,68,72,89,125`). MCP tool errors should be structured content the model can branch on (`{error_type, message, retriable, hint}`), not a bare string. A 404 "no such document" (`service.py:72,89`) gives the agent no recovery path (re-fetch? the id expired? — it can't tell).
- **No pagination, no resources, no output schema.** `select_all`/`project` can return unbounded rows (`collection.py:211`); there is no cursor, no `limit`/`offset` at the tool boundary (they exist on `select_all` — `surfaces.py:148` — but aren't surfaced as a tool contract), and the `/execute` response is untyped `{"rows": <anything>}` (`service.py:81`). MCP structured output wants a declared shape.
- **Auth is a single static bearer token** (`service.py:55-57`) — fine for a demo, but MCP 2025-06-18 moved to OAuth Resource-Server semantics for hosted servers; a static token is acceptable only for local/self-host.
- **The unbounded server stores are an agent-lifetime hazard.** `app.state.docs`/`app.state.sessions` grow forever with no TTL/eviction (`service.py:52-53`; noted in `docs/assessment.md` P0-3). A long-running agent session leaks the host. If handles/resources are the model, they *must* expire, and the tool must tell the agent when they have.
- **`/crawl` is a 501** (`service.py:122-125`) — so the headline agent workflow ("crawl this site → give me markdown") is absent at the API tier.

### 4.3 What a *good* MCP over this library looks like
Not "wrap `/execute`." Instead, wrap the **task verbs** (Mode 3) as MCP tools with schemas + structured output, and use resources only for large bodies. See the concrete tool list in §6.2. The library is *well-positioned* to do this because the executor already produces markdown/rows/handles; the work is a thin MCP adapter over new task endpoints, plus structured errors and TTL'd handles.

### Verdict for Mode 2
The *transport* is ready (FastAPI, a wire format, server-side execution — `service.py`, `remote_core.py:72`), but the *tool shape* is wrong. Build Mode 3 first, then the MCP is a mechanical wrapper that inherits good tools. Investing in MCP before Mode 3 means shipping an `execute(plan)` tool that models will misuse.

---

## 5. Mode 3 — a small Python-callable tool set

**Rating: ADEQUATE today via the existing surface / STRONG once a thin verb layer is added. This is the recommended investment.**

### Why this mode wins
- **The primitives already exist and already return LLM-ready output.** `summary(url) → {title, markdown, ok}` (`document_core.py:203`), `render("markdown"|"text"|"elements"|"links")` (`document_core.py:290-310`), `extract(**cols).project() → list[dict]` (`collection.py:190,211`), link discovery via `render("links")` (`document_core.py:296`). A tool layer is *assembly*, not new capability.
- **It hides everything an LLM gets wrong.** No `doc`/`ref` globals, no `.collect()` vs `execute`, no eager/lazy split, no Plan IR. The model calls `extract(url, {"title": ".title", "price": ".price"})` and gets rows. The lazy/plan machinery becomes an implementation detail the *tool* uses server-side.
- **It matches the shipped competition.** Firecrawl's MCP is exactly this: ~5 tools (scrape→markdown, crawl, search, map/sitemap, schema-extract). Tavily is search/extract/crawl/map. This is the proven shape for LLM web access; the repo already has the rendering quality primitives to match (modulo markdown richness — `document_core.py:77` `_md_blocks` lacks tables, per `docs/assessment.md` §2.2).
- **Token budget fits by construction.** Returning markdown/rows instead of raw HTML is the whole point; `render("text", main_content_only=True)` (`document_core.py:301`) already strips nav/chrome via `_main_container`/`_NOISE`. Add per-tool truncation + a `response_format: "concise"|"full"` enum (Anthropic guidance) and outputs stay within context.

### What's missing to be excellent
- **A stable, task-oriented signature set** (they don't exist yet — the only "verbs" are the client backings `fetch`/`ref`/`summary`/`search`-as-expression, `surfaces.py:261-264`). Define 6 verbs (§6.1).
- **Schema-guided extraction.** Today `extract` takes sub-*expressions* and `project()` returns `list[dict]` (`collection.py:190,211`); an LLM wants to pass a flat `{field: selector}` (or a JSON Schema) and get validated typed rows. This is `project(model)`, sketched but unimplemented (per `docs/assessment.md` P1-6). It is the single most valuable tool feature.
- **Structured errors + no raw handles.** Return `{ok, error:{type,message,retriable,hint}}`, and inline content by default (not `_RemoteDoc`).
- **Crawl.** The one verb with no backing (`service.py:122` 501). Even a bounded BFS over `render("links")` would unlock the headline workflow.

### Verdict for Mode 3
This is where the library's real assets (clean execution, markdown/rows, server-side runs) convert directly into LLM end-user value with the least new surface area and the lowest generation-failure rate. **Build this first.**

---

## 6. Concrete sketch for the recommended mode (Mode 3, and its MCP packaging)

### 6.1 The six tools (function-calling schemas)

Design rules applied: few high-signal verbs; each is one complete workflow; token-efficient defaults with an escape hatch; actionable errors; namespaced `web_*`. All return content inline (no handles) with optional truncation.

```json
[
  {
    "name": "web_fetch",
    "description": "Fetch a URL and return it as clean, LLM-ready markdown (JS-rendered if needed). Use when you have a known URL and want its readable content. For structured fields, use web_extract instead.",
    "input_schema": {
      "type": "object",
      "properties": {
        "url": {"type": "string", "description": "Absolute http(s) URL."},
        "format": {"type": "string", "enum": ["markdown", "text", "html"], "default": "markdown"},
        "main_content_only": {"type": "boolean", "default": true, "description": "Strip nav/footer/boilerplate."},
        "browser": {"type": "boolean", "default": false, "description": "Render with a real browser for JS-heavy pages. Slower."},
        "max_chars": {"type": "integer", "default": 8000, "description": "Truncate output; a truncation marker + continuation cursor are returned if exceeded."}
      },
      "required": ["url"]
    }
  },
  {
    "name": "web_extract",
    "description": "Extract structured rows from a page. Give a CSS/XPath selector for the repeating item, and a flat map of column -> selector. Returns validated JSON rows. Use this instead of fetching HTML and parsing it yourself.",
    "input_schema": {
      "type": "object",
      "properties": {
        "url": {"type": "string"},
        "item_selector": {"type": "string", "description": "Selector matching each repeating item, e.g. '.card'. Omit to extract one row from the whole page."},
        "fields": {
          "type": "object",
          "description": "column name -> selector (append '@href'/'@src' for an attribute, else text).",
          "additionalProperties": {"type": "string"}
        },
        "limit": {"type": "integer", "default": 50},
        "offset": {"type": "integer", "default": 0}
      },
      "required": ["url", "fields"]
    }
  },
  {
    "name": "web_search",
    "description": "Search the web (or a site) and return ranked results with title, url, and a short snippet. Use to discover URLs before fetching/extracting.",
    "input_schema": {
      "type": "object",
      "properties": {
        "query": {"type": "string"},
        "site": {"type": "string", "description": "Optional: restrict to a domain."},
        "limit": {"type": "integer", "default": 5}
      },
      "required": ["query"]
    }
  },
  {
    "name": "web_crawl",
    "description": "Crawl a site starting from a URL, following same-origin links up to a depth/page budget, returning markdown per page. Use for 'summarize this docs site' style tasks. Returns a page cursor for continuation.",
    "input_schema": {
      "type": "object",
      "properties": {
        "start_url": {"type": "string"},
        "max_pages": {"type": "integer", "default": 20},
        "max_depth": {"type": "integer", "default": 2},
        "include": {"type": "string", "description": "Optional path glob to include, e.g. '/docs/*'."},
        "cursor": {"type": "string", "description": "Continuation cursor from a prior call."}
      },
      "required": ["start_url"]
    }
  },
  {
    "name": "web_screenshot",
    "description": "Render a page (or an element) with a real browser and return a screenshot. Use only when a visual is required; prefer web_fetch for text.",
    "input_schema": {
      "type": "object",
      "properties": {"url": {"type": "string"}, "selector": {"type": "string"}, "full_page": {"type": "boolean", "default": false}},
      "required": ["url"]
    }
  },
  {
    "name": "web_interact",
    "description": "Drive a live browser page through a short action script (click/type/wait) and return the resulting page content. Use for flows behind a button/login. Actions auto-wait.",
    "input_schema": {
      "type": "object",
      "properties": {
        "url": {"type": "string"},
        "actions": {
          "type": "array",
          "items": {"type": "object", "properties": {
            "op": {"type": "string", "enum": ["click", "write", "wait_for"]},
            "selector": {"type": "string"}, "text": {"type": "string"}
          }, "required": ["op"]}
        },
        "return_format": {"type": "string", "enum": ["markdown", "text"], "default": "markdown"}
      },
      "required": ["url", "actions"]
    }
  }
]
```

**Example successful `web_extract` result (structured output, inline, token-lean):**
```json
{
  "ok": true,
  "url": "https://shop.example/",
  "row_count": 2,
  "rows": [
    {"title": "Aeropress", "price": "$39", "link": "/items/1"},
    {"title": "Grinder",   "price": "$129", "link": "/items/2"}
  ],
  "truncated": false
}
```

**Example *recoverable* error result (what the service must start returning):**
```json
{
  "ok": false,
  "error": {
    "type": "selector_no_match",
    "message": "item_selector '.card' matched 0 elements",
    "retriable": false,
    "hint": "Call web_fetch(url, format='html') to inspect the DOM, or try a broader selector."
  }
}
```
Contrast today's failure: `HTTPException(422, detail="<stringified ValueError>")` (`service.py:68`) or a bare Python `AttributeError` — neither tells the agent what to do next.

Internally, each verb **builds the plan itself** and runs it via the existing executor — e.g. `web_extract` assembles the same chain `demo.py:244-255` records, so no LLM ever touches `Expr`/`Plan`. This is a pure adapter over `collection.py` + `document_core.py`.

### 6.2 The MCP packaging (Mode 2, built on 6.1)

- **Tools:** the six `web_*` verbs above, verbatim, each with an **output schema** (structured tool output, MCP 2025-06-18) matching the result shapes.
- **Resources:** `web://doc/{id}` for a large fetched body an agent chose to defer, and `web://crawl/{job}/page/{n}` for crawl pages — addressable, TTL'd (fixing `service.py:52` unbounded stores). Resources, *not* the default return path (invert today's `_RemoteDoc` default — `remote_core.py:37`).
- **Pagination:** opaque `cursor` on `web_search`/`web_crawl`/`web_extract` (MCP cursor semantics), replacing "one round-trip per op."
- **Errors:** structured content (`{error:{type,message,retriable,hint}}`), never bare `detail` strings.
- **Auth:** keep the bearer token (`service.py:55`) for self-host; document OAuth-RS as the hosted path.

### 6.3 For completeness — a Mode-1 emitted plan (why it's the *last* choice)
The same two-item extraction as an LLM-emitted Plan IR is ~40 lines of nested `{kind,name,args,kwargs}` with paired `get`/`call` steps and `Arg.plan` sub-trees (structure per `plan.py:16-50`, calling convention per `executor.py:111`). It is correct and auditable but 5-10× the tokens of the `web_extract` call above and far more error-prone to author. Keep it as the compiler's output and the audit log — not the model's input.

---

## 7. Prioritized changes to make the library LLM-native

### P0 — the minimum to be safely drivable by an autonomous agent
1. **Add the Mode-3 task-verb layer** (`web_fetch/extract/search/crawl/screenshot/interact`) as service endpoints + thin Python functions that build plans internally and return markdown/rows inline. *Files:* new `webclient/tools.py` + endpoints in `service.py`; reuses `document_core.py:203,290`, `collection.py:190,211`. **This is the highest-leverage change.**
2. **Structured, actionable errors** everywhere a tool can fail. Replace `HTTPException(status_code, detail=str)` (`service.py:57,68,72,89,125`) and raw `WebError` with `{ok:false, error:{type,message,retriable,hint}}`. Surface authoring errors (`expr.py:91`) the same way. Without this, agents cannot recover deterministically.
3. **A URL/host safety boundary.** `validate_names` (`plan.py:55`) is not enough. Add an allow/deny host policy (block link-local/private ranges by default — there is *no* such guard in `webclient/` today), a per-plan op/fan-out budget (cap `executor.py:94` fan-out and total resolves), and a browser/screenshot opt-in gate. Required before any hosted LLM emission (Modes 1 & 2).
4. **Bound + TTL the server stores** (`service.py:52-53`) and time-out the remote client (`remote_core.py:62`) — an agent loop will otherwise leak the host. (Also `docs/assessment.md` P0-3.)

### P1 — make the tools *excellent* for models
5. **Schema-guided `extract` / typed `project(model)`** (sketched, unimplemented per `docs/assessment.md` P1-6; today `collection.py:211` returns untyped `list[dict]`). Let the model pass `{field: selector}` or a JSON Schema and get validated rows. Single most valuable tool feature.
6. **Token controls on every tool output:** `max_chars` truncation with a continuation cursor, `response_format: concise|full`, and cursor pagination on list-returning tools (`web_search`/`web_extract`/`web_crawl`). Aligns with Anthropic token-efficiency guidance + MCP pagination.
7. **Implement `crawl`** as a bounded BFS over `render("links")` (`document_core.py:296`), replacing the 501 (`service.py:122`). Unlocks the headline agent workflow.
8. **Invert the handle default** for agentic callers: return content inline; use `web://doc/{id}` resources only for large deferred bodies (today `_RemoteDoc` makes handles the default and every field a round-trip — `remote_core.py:37`).

### P2 — polish, MCP, and the power-user DSL
9. **Ship the MCP server** as a wrapper over the P0/P1 tools with input+output schemas, resource URIs, and cursor pagination (MCP 2025-06-18). Mechanical once §6.1 exists.
10. **Richer markdown** (tables, nested lists, code fences) in `_md_blocks` (`document_core.py:77`) to match Firecrawl output quality — directly improves every `web_fetch`/`web_crawl` result an LLM reads.
11. **If exposing Mode 1 at all**, expose the *Python chain* (not raw Plan JSON), and first collapse the eager/lazy vocabulary and the double trigger, and namespace the roots (`E.doc` not global `doc`) — `expr.py:226`, `surfaces.py:352`. Keep the Plan IR as the compile target + audit log.

---

## 8. Bottom line

The library has built the hard part — a clean, uniform, serializable execution kernel — and left unbuilt the easy, high-value part: a task-shaped surface an LLM can drive. Its markdown/rows rendering and server-side execution are already the right raw materials; what's missing is a six-verb tool layer that hides the plan machinery, returns structured/truncated results, fails with recoverable errors, and refuses to fetch internal hosts. Build **Mode 3 first**, wrap it as **MCP second**, and keep the **Plan IR as an internal compile-and-audit format rather than an LLM emission target**. The single highest-leverage move is the server-side task-verb layer; everything else in Modes 1 and 2 either hangs off it or is a niche. Protect the plan-IR kernel (it is the differentiator, per `docs/assessment.md`), but stop asking a model to speak it.

---

## Sources

- [MCP — Tools (2025-06-18 spec)](https://modelcontextprotocol.io/specification/2025-06-18/server/tools)
- [MCP 2025-06-18 spec update: structured output, elicitation, resource links (ForgeCode)](https://forgecode.dev/blog/mcp-spec-updates/)
- [MCP — Pagination (opaque cursor semantics)](https://modelcontextprotocol.io/specification/2025-03-26/server/utilities/pagination)
- [MCP resources (Speakeasy)](https://www.speakeasy.com/mcp/core-concepts/resources)
- [Anthropic — Writing effective tools for AI agents](https://www.anthropic.com/engineering/writing-tools-for-agents)
- [Anthropic — Effective context engineering for AI agents](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents)
- [Firecrawl official MCP server (scrape/crawl/search/map/extract)](https://github.com/firecrawl/firecrawl-mcp-server)
- [Firecrawl — scrape a website to markdown for LLMs](https://www.firecrawl.dev/blog/scrape-a-website-to-markdown)
- [Tavily / Firecrawl alternatives overview (Bright Data)](https://brightdata.com/blog/ai/firecrawl-alternatives)
</content>
</invoke>
