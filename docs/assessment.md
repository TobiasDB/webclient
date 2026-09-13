# Principal-Engineer Assessment: `webclient`

*Subject:* `/home/zeus/git/web-client` @ `refactor/async-core-backends` (b6bdb80)
*Scope:* Is this a **usable AND genuinely useful** browser-as-a-service / web-query-and-crawling foundation that delivers value to end users?
*Method:* Full read of `webclient/` + `example.py` (design intent) + `demo.py` + `PLAN.md`/`spec.py`/`ISSUES.md`; suite run (147 passed, 7.6s); `gen_stubs --check` green; package = 4,443 LOC. Comparison against 2025/2026 practice (Scrapy, Crawlee for Python, Playwright, Firecrawl, Browserless). **No code was changed.**

---

## 1. Executive summary

**Verdict: a promising, unusually clean *architecture* that is not yet a useful web-scraping/crawling foundation for end users.** The core mechanism — one lazy expression language recorded into a serializable plan IR, executed by one async walker, over cores whose behaviour comes from swappable "backings" — is genuinely elegant and delivers on the "one mechanism for sync/async/remote/lazy" thesis (`example.py`). But almost everything that makes a scraping tool *valuable in the field* (crawling, retries/backoff, proxy rotation, anti-bot, rate-limiting/politeness, real streaming, schema/LLM extraction) is either deferred, stubbed, or contradicted by the code. It is a strong *engine kernel* and a weak *product*.

- **Usable?** Adequate. The authoring surface (`ref.resolve().select_all(".card").extract(...).filter(...).project()`) reads well and IDE-autocompletes via generated stubs; the lazy/eager/async/remote surfaces are the same API. But there are two competing evaluation triggers (`.collect()` vs `wc.execute()`), no user-facing docs (only internal `PLAN.md`), global roots that shadow locals (acknowledged, `PLAN.md:456`), and an `.attr("text")`-vs-`.text` inconsistency between the lazy and eager tiers.
- **Useful?** Weak-to-adequate *today*. It fetches, selects (css/xpath/json-path), renders to markdown/text/elements/links, drives a real browser (click/write/wait/screenshot), keeps cookie sessions, and runs remotely. That is a real feature set — but it is roughly "httpx + lxml + a thin Playwright wrapper," and the crawl/extraction/anti-bot layer that differentiates Firecrawl/Crawlee/Browserless is absent (`crawl` returns HTTP 501, `service.py:122`).
- **Differentiated value is real but structural, not yet end-user-facing:** the plan IR *is* the wire format, so sync == async == remote == lazy with no parallel implementations (`plan.py`, `executor.py`, `remote.py:72`). That is a better factoring than Scrapy's middleware stack or Crawlee's request-handler model. It buys maintainability, not (yet) user outcomes.

**Top 3 strengths**
1. **One recorder → one plan IR → one executor, reused for eager/lazy/async/remote/service.** `Expr` records (`expr.py:41-55`), `Plan` is a pydantic wire model with a name-safety validator (`plan.py:54-71`), and `RemoteWebClientCore.execute` is literally "POST the same plan" (`remote.py:72-90`). No drift between modes by construction.
2. **Uniform capability/dispatch kernel (`WebCore` + `Backing`).** Every core chooses applicable backings and dispatches ops to them (`web_core.py:58-101`); adding a medium or an op is local and the generator derives the typed surface from the backing signature (`scripts/gen_stubs.py`). Clean, testable, low-ceremony extension.
3. **Disciplined async lifecycle.** Per-client engine loop in a daemon thread with a re-entrancy guard (`loop.py:47-52`), cancellable stream bridge that always tears down its pump (`loop.py:92-112`), and lease-leak fixes on every failure path (`client_core.py:360`, `pool.py:87`). The git log shows this was hardened deliberately.

**Top 5 gaps**
1. **"Streaming" does not stream.** `astream` computes the *entire* result via `aevaluate` and then iterates it (`executor.py:194-202`); `fan_out` allocates `[None]*len(items)` and awaits all tasks before returning (`executor.py:228-244`). The demo/`PLAN.md` claim "rows stream as they complete" — false at the collection level. This caps memory and latency behaviour and undermines any crawl-scale use.
2. **No crawling foundation at all.** No URL frontier/queue, no dedup, no robots.txt, no depth/breadth control, no politeness/rate-limiting. `crawl` is 501 (`service.py:122-125`). This is the single biggest "useful browser-as-a-service" gap vs Crawlee/Firecrawl/Scrapy.
3. **No resilience layer for the real web.** Transport retries default to **0** (`engine/clients.py:57`), a single optional proxy with **no rotation pool** (`engine/clients.py:39,108`), no anti-bot/stealth, no per-host rate limiting, no caching. Against 2026 anti-bot systems this fetches like a bot and gives up on the first blip.
4. **Server/remote tier leaks and is chatty.** The service stores every returned document in an unbounded `dict` with no TTL/eviction (`service.py:51-52`); the remote client uses a timeout-less `httpx.Client` (`remote.py:63`) and each op on a returned handle is a separate round-trip (`remote.py:38-44`). Fine for a demo, unsafe under load.
5. **The whole runtime is `Any`-typed; type-safety lives only in generated stubs.** Runtime dispatch is stringly-typed `__getattr__`/`dispatch` (`surface.py:69`, `collection.py:130`, `surfaces.py:269`); the "types" are a *separate* generated artefact (`models.py`, `surfaces.py` `TYPE_CHECKING` blocks). They are reconciled by `gen_stubs --check`, but a runtime that accepts names the stubs never declared (and vice-versa) is only caught if a test exercises it. Plus fragile spots: cookie parsing splits `Set-Cookie` on `", "` (`session_core.py:100`) which breaks on `Expires=Wed, 09 Jun ...`.

---

## 2. Per-axis findings

### 2.1 Ease of use — **adequate**

**What's good.** The lazy authoring chain is legible and composes the way Polars/Django-ORM users expect: `reference(url).resolve().select_all(".card").extract(title=doc.select(".title").attr("text")).filter(doc.field("price") != "").project()` (`demo.py:244-255`). Operators are overloaded to *record* (`Expr.__eq__` → an `op` step, `expr.py:61`), and the "pit of failure" is closed loudly: `bool()`/`len()`/`iter()` on a lazy expr raise a message that names the recordable form (`expr.py:90-104`) — a textbook "make illegal states unrepresentable" touch. `attr("href")` narrows to a `Reference` via a `Literal` overload (`document_core.py:352`), so link-following is type-visible. Because the runtime is `__getattr__` dispatch but the *stubs* are generated from the backing signatures, IDE autocomplete and mypy/pyright both work (`test_typing.py` runs both checkers) — you get discoverability without hand-maintaining a facade.

**What hurts.**
- **Two evaluation triggers.** `.collect()` (`expr.py:107`) and `wc.execute(plan)` (`surfaces.py:352`) both run a plan; `demo.py` uses both within a few lines (`demo.py:263` vs `:269`). New users must learn when each applies (bound vs unbound client, streaming). One primary trigger with the other as a documented alias would be a "pit of success" improvement.
- **Lazy/eager vocabulary split.** In an eager, materialised `Document` you write `.text`/`.title` (properties); inside a lazy plan you write `.attr("text")` because `text` special-cases the attr op (`document_core.py:364`). The demo shows both (`demo.py:184` eager `page.title`, `:246` lazy `.attr("text")`). This is a discoverability tax.
- **Global roots shadow locals.** `doc`, `ref`, `many` are module globals (`expr.py:226-228`); the team already had to rename demo variables to avoid collisions (`PLAN.md:456-459`). A `from webclient import expr as E; E.doc` idiom or a `cols`-style namespace would be safer.
- **No user docs.** There is no README/tutorial/API reference — only `PLAN.md` (an internal changelog) and `spec.py`/`example.py` (sketches). For a library whose thesis is ergonomics, the absence of a "getting started" is a real usability gap. `demo.py` is the de-facto tutorial and is good, but it is not documentation.

*Compared to expectations:* the authoring feel is closer to Polars/SQLAlchemy-lazy than to Scrapy's callback/`yield Request` model — a genuine ergonomic win for one-shot extraction. It is worse than Firecrawl's "one call, get markdown" for the trivial case (you still assemble a chain), and worse than Playwright's imperative clarity for interactive flows.

### 2.2 Usefulness / value — **weak-to-adequate**

**Jobs it does well today.**
- **Fetch + structured selection.** css/xpath over lxml with element-as-document nesting (`document_core.py:331-350`), json dotted-path selection (`document_core.py:429-436`), attribute/text extraction with link-narrowing.
- **Page → markdown / text / elements / links.** `render("markdown"|"text"|"elements"|"links")` (`document_core.py:277-310`) and `summary(url) → {title, markdown}` (`document_core.py:203-210`) directly target the Firecrawl "LLM-ready page" use case.
- **Live browser interaction.** Real Playwright click/write/wait/screenshot with auto-wait, console + DOM-mutation capture onto the document, and action-chain replay via `reload()` (`core/live.py`, `client_core.py:364`).
- **Cookie sessions + remote execution + a FastAPI service.** Same surface, executed server-side (`remote.py`, `service.py`).

**What's missing to be *genuinely* useful (vs just using httpx+lxml or Playwright directly):**
- **Crawling.** No frontier, no dedup, no robots.txt, no depth/limit controls, no sitemap discovery. `crawl` is 501 (`service.py:122`). This is the headline capability of Crawlee/Firecrawl and the reason people reach for a "crawling foundation."
- **Schema / LLM-guided extraction.** The 2025-2026 differentiator (Firecrawl `extract` with a zod/JSON schema; "give me `{title, price}` typed") is absent. `project()` returns `list[dict]` (`collection.py:211`); `spec.py:207` sketched `project(model)` and even a Polars DataFrame return, but neither is implemented.
- **Resilience / evasion.** No retry/backoff policy (transport `retries=0`, `clients.py:57`), no proxy rotation, no stealth/fingerprint, no rate limiting/politeness, no HTTP caching or conditional requests. Real sites will block or flake and the library has no answer.
- **Markdown quality.** `_md_blocks` handles headings/p/lists/pre/blockquote/img but not tables, nested lists beyond one level, or definition lists (`document_core.py:77-100`). Firecrawl's markdown is markedly richer.

**The differentiated value of "one mechanism for sync/async/remote/lazy"** is *real and rare* — but it is **infrastructure value, not user value**. It means a feature written once works in all four modes and serializes to a service for free. That is a strong foundation property (better than Scrapy, where async/remote are bolt-ons). It does not, by itself, get a user their data; the features that do are the missing ones above.

*Net:* as a **foundation** the primitive set is adequate; as a **product** it is currently a thinner, less-resilient Playwright/lxml wrapper without the crawl+extract+evade layer that makes commercial tools worth adopting.

### 2.3 Scalability — **weak**

- **Concurrency model is sound but small-scale.** One asyncio loop per client in a daemon thread (`loop.py`), bounded fan-out via a semaphore-capped pool (`pool.py:65-91`), fan-out width taken from the http limit (default 8-10, `executor.py:44-47`, `client_core.py:156-159`). Good for "resolve a page, fan out over 8 elements." No horizontal/distributed story (single process), which is fine for a foundation but should be stated.
- **Streaming is not real (critical).** `astream` awaits the *whole* `aevaluate` then yields (`executor.py:194-202`); `fan_out` builds a full results list and awaits every task before returning (`executor.py:228-244`). So a 10k-element collection is fully materialised in memory before the first row is "streamed." The `loop.stream` bridge (`loop.py:58-112`) is genuine cancellable plumbing, but its source is already complete. This directly contradicts the "rows stream as they complete" claim (`PLAN.md:335`) and is the main scale blocker.
- **No backpressure into fan-out.** The bounded queue only sits between an already-materialised list and the sync consumer; there is no incremental pull that limits how much of a crawl is in flight/among results.
- **Remote tier is chatty and unbounded.** Each op on a `_RemoteDoc` is one HTTP round-trip (`remote.py:38-44`); the client has no timeout (`remote.py:63`); the server keeps every document forever in `app.state.docs` (`service.py:51-52`) and every session in `app.state.sessions` with no reaper (session TTL exists on the core, `session_core.py:38`, but the service never evicts). A long-running service leaks memory.
- **Resource limits.** Page pool capped at 4, http at 10 (`client_core.py:159`) with a 60s acquire timeout that raises cleanly on exhaustion (`pool.py:70-78`) — a reasonable local default, but there is no global concurrency budget across sessions and no per-host cap.

*Where it breaks under load:* any workload that is genuinely "crawl N thousand pages and stream results" — it will materialise everything, hold every server-side document, and issue no retries when the target rate-limits it.

### 2.4 Extensibility — **adequate-to-strong**

- **New backing / op:** subclass `Backing`, declare `provides`/`props`/`gate`/`applies`, add to a core's `BACKINGS` tuple (`web_core.py:31-42`, `document_core.py:548`). Dispatch and capability-gating are automatic. This is the best-factored part of the codebase.
- **New backend (remote/other transport):** subclass `WebClientCore` and override `execute` — "remote is a core-swap" (`remote.py:50`, `72`). Excellent seam.
- **New renderer:** subclass `Renderer`, `wc.use(r)`; backings consult the override table before their built-in (`surfaces.py:183`, `client_core.py:173`, `document_core.py:453`). Clean.
- **Friction — the generation step.** `gen_stubs.py` derives the eager + lazy + collection-lift stubs from backing signatures in one walk. This is a *help* for consistency (one source of truth, drift fails CI) but a *friction* for contributors: annotations must resolve at generation time or it hard-errors (`gen_stubs.py:106-127`), and any new referenced type must be registered in `_NS` (`gen_stubs.py:51`). The tier-mapping logic (`_render`/`_classify`, `gen_stubs.py:66-174`) is non-trivial to learn. A contributor adding an op with an unusual return type will hit this wall.
- The uniform kernel means most extensions are additive and local — a real strength for a foundation.

### 2.5 Maintainability — **adequate**

- **Clarity:** module docstrings are unusually good and honest; boundaries (core / expr / plan / executor / surface / engine / pool) are clean; the dispatch kernel is small and uniform.
- **Type-safety story is split-brain.** The runtime is `Any` + stringly `__getattr__` dispatch (`surface.py:69`, `collection.py:130`, `surfaces.py:269-280`); the types are a *generated* artefact. `PLAN.md:809` admits each op is stated three times (executable op, eager stub, lazy stub). `gen_stubs --check` keeps them aligned, but the design intent in `example.py:58-93` (an `Expr` that *resolves return types at runtime* via `_attr_return_type`) was **abandoned** in favour of codegen — worth recording that the north star's central trick isn't what shipped.
- **Coupling via local imports.** Nearly every function imports its collaborators inline to dodge cycles (e.g. `executor.py` re-imports `Collection`/`wrap` in multiple functions; `client_core.py` throughout). It works, but it is a smell that the module graph is more tangled than the layering suggests, and it hides real dependencies from tooling.
- **Architectural churn.** `PLAN.md` + the memory index show a refactor → rewrite → full-lazy → "surface = stubs" sequence, with a line-count "budget" repeatedly missed (target 3,871; actual 4,443; `PLAN.md:809-814` concedes the target predates the architecture). The obsession with LOC over interfaces is itself a maintainability risk: it has driven several ground-up reshuffles. The engine has been stable *functionally* (tests stayed green), but the surface has been rewritten more than once.
- **Tests:** 147, broad (fetch/select/render/session/browser/remote/service/stream-lifecycle/evaluate/typing). `test_typing` runs mypy **and** pyright; `test_stream_lifecycle` exercises cancellation. Good coverage of the mechanism. Thin on adversarial/robustness cases (malformed cookies, redirects, huge collections, remote timeouts).

### 2.6 Correctness / robustness — **adequate**

**Strong:**
- Error policy is coherent: RAISE by default, RETURN lenient, contextvar-scoped so `extract`/`filter` degrade per-field instead of aborting a plan (`errors.py:34-49`, `collection.py:165`).
- Lifecycle/leaks: `async with lease` on the http path (`client_core.py:230`), page released on any failure (`client_core.py:360`), permit released on create failure (`pool.py:87`), stream pump always cancelled and awaited (`loop.py:92-112`), loop drains tasks on stop (`loop.py:114-132`).
- Thread-safety: single loop thread; the sync facade refuses to run from the loop thread (`loop.py:47-52`) — prevents the classic bus-handler deadlock.
- Nice guardrails: xpath `text()`/`/@attr` selectors rejected with a message pointing at `.attr()` (`document_core.py:322-325`).

**Weak / risky:**
- **`Set-Cookie` parsing splits on `", "`** (`session_core.py:98-104`), which corrupts any cookie with a comma in an `Expires=`/date attribute — a common real-world case. Should parse per `Set-Cookie` header, not by string-splitting a joined value.
- **Remote client has no timeout** (`remote.py:63`) — a stalled server hangs the caller indefinitely.
- **Streaming claim is false** (see 2.3) — a correctness-of-documentation issue as much as a scale one.
- **`fan_out` first-exception-wins** discards sibling failures when unwrapping the `ExceptionGroup` (`executor.py:239-243`) — acceptable, but sibling errors are lost for diagnostics.
- **`NameScope` isn't locked** (`client_core.py:40-68`); it is only mutated on the loop/construction thread today, but nothing enforces that invariant if a session is created off-thread.
- No robots.txt / politeness — a correctness concern for anything calling itself a crawler.

---

## 3. Comparative analysis

Ratings are for *this library today* against each tool's shipped capability on the axis. "◐" = partial.

| Axis | **webclient (this repo)** | Scrapy | Playwright(-python) | Firecrawl | Browserless |
|---|---|---|---|---|---|
| **Fetch + parse (HTML/JSON)** | ✅ httpx + lxml + json-path, element-as-doc nesting (`document_core.py`) | ✅ parsel/lxml, mature | ◐ full DOM but you script extraction | ✅ returns clean markdown/HTML/JSON | ✅ via BrowserQL/Playwright |
| **Page → markdown / LLM-ready** | ◐ basic markdown/elements/links (`document_core.py:277`), no tables/nested | ✗ (DIY) | ✗ (DIY) | ✅ **best-in-class**, main product | ◐ via content APIs |
| **Schema / LLM extraction** | ✗ `project()`→`list[dict]` only (`collection.py:211`) | ✗ (Items, manual) | ✗ | ✅ schema-guided JSON extract | ◐ |
| **Browser automation** | ◐ click/write/wait/screenshot + capture (`core/live.py`) | ✗ (needs scrapy-playwright) | ✅ **reference impl** | ◐ (managed) | ✅ managed, stealth |
| **Crawling (frontier/dedup/robots)** | ✗ 501 (`service.py:122`) | ✅ **core strength** | ✗ | ✅ crawl endpoint | ◐ |
| **Retries / backoff** | ✗ `retries=0` (`clients.py:57`) | ✅ RetryMiddleware | N/A | ✅ managed | ✅ managed |
| **Proxy rotation** | ✗ single proxy, no pool (`clients.py:39`) | ✅ middleware ecosystem | ◐ per-context | ✅ managed | ✅ connection-level, sticky/rotate |
| **Anti-bot / stealth** | ✗ | ◐ (add-ons) | ◐ (add-ons) | ✅ | ✅ **built-in stealth/CAPTCHA** |
| **Rate limiting / politeness** | ✗ | ✅ AutoThrottle | N/A | ✅ | ✅ |
| **Concurrency / streaming** | ◐ bounded fan-out but **not truly streaming** (`executor.py:194`) | ✅ async reactor, streams items | ◐ | ✅ managed | ✅ auto-scale, load-balanced |
| **Sync + async + remote from one API** | ✅ **differentiator** (`remote.py:72`) | ✗ (async only, no remote) | ◐ sync+async, no remote | N/A (SaaS) | N/A (SaaS) |
| **Self-host service tier** | ◐ FastAPI `/execute`, but unbounded store (`service.py:51`) | ✗ (Scrapyd separate) | ✗ | ◐ (SaaS + limited OSS) | ◐ (self-host option) |
| **Typed API / IDE support** | ✅ generated stubs, mypy+pyright gated | ◐ | ✅ | N/A | N/A |

**Cited lessons to adopt:**
- **Crawlee's `AdaptivePlaywrightCrawler`** decides HTTP-vs-browser per request by comparing outputs and falling back to the browser only when needed — a perfect fit for this repo's `resolve(browser=...)` seam; make it *automatic* rather than a caller flag. ([Crawlee for Python](https://crawlee.dev/python/docs/guides/adaptive-playwright-crawler), [Apify announcement](https://blog.apify.com/announcing-crawlee-for-python/))
- **Firecrawl's "one call → markdown or schema-guided JSON"** is the value users pay for; the repo's `summary()`/`render("markdown")` are the right primitives but need schema-guided extraction and richer markdown. ([Firecrawl scrape tutorial](https://www.firecrawl.dev/blog/mastering-firecrawl-scrape-endpoint), [scrape-to-markdown](https://www.firecrawl.dev/blog/scrape-a-website-to-markdown))
- **Browserless: proxy/region bound at session creation, sticky-by-default with opt-out rotation, stealth without middleware.** The repo's session model is the natural home for proxy binding, and its `Backing` seam is the natural home for a stealth backing. ([Browserless proxies](https://docs.browserless.io/browserql/bot-detection/proxies), [anti-detection 2026](https://www.browserless.io/blog/anti-detection-techniques-2026-guide))
- **Scrapy's separable middlewares (retry, throttle, proxy)** map cleanly onto this repo's backing/dispatch kernel — resilience should be pluggable backings, not hard-coded. ([Scrapy proxy middleware](https://dev.to/onlineproxy_io/scrapy-middleware-engineering-resilient-proxy-rotation-systems-3cfi))

*Where this library is genuinely better:* the **single-mechanism** factoring (a plan IR that is simultaneously the eager path, the lazy path, and the wire format) is cleaner than any of the four. Scrapy's remote (Scrapyd) and Playwright's lack of a remote story both require parallel machinery; here it is one `execute` override. That is the asset worth protecting.

---

## 4. Prioritized recommendations

### P0 — make the claims true and the tier safe (correctness/trust)
1. **Make streaming actually stream, or stop claiming it.** Rework `fan_out`/`astream` so rows are yielded as each element completes (an `asyncio.as_completed`/queue-fed generator bounded by the pool) instead of `await`-ing the full list (`executor.py:194-244`). If real streaming is out of scope now, change the demo/`PLAN.md` wording — a false capability claim is worse than an honest gap. *(High value, contained change.)*
2. **Fix `Set-Cookie` parsing.** Parse per header (or via `http.cookies.SimpleCookie`) instead of `raw.split(", ")` (`session_core.py:98-104`). Add a regression test with a dated `Expires=`. *(Small, prevents silent session breakage.)*
3. **Bound the service and time-out the remote client.** Add TTL/LRU eviction to `app.state.docs`/`app.state.sessions` (`service.py:51`); give `RemoteWebClientCore._http` an explicit timeout (`remote.py:63`). *(Prevents the two most likely production incidents.)*

### P1 — become resilient enough for the real web (usefulness)
4. **Add a resilience backing:** retry-with-backoff (transport + 429/503), per-host rate limiting/politeness, and conditional-request/response caching. The `Backing` kernel is the right home; `HTTPXFactory` already threads `retries`/`proxy` (`clients.py:44-71`) — wire policy through, don't default to 0.
5. **Proxy rotation.** Turn the single `proxy` on `HTTPXFactory` (`clients.py:108`) into a rotating pool bound at session creation (Browserless model), addressable per-`Reference`.
6. **Schema-guided extraction / typed `project`.** Implement `project(model)` (already sketched, `spec.py:77,207`) to validate rows into a pydantic model (and optionally a DataFrame). This is the highest-leverage *feature* for LLM/RAG users and reuses the existing extract machinery.

### P2 — become a crawler and a documented product (adoption)
7. **Implement `crawl` as a plan over the existing seams:** a bounded frontier (dedup by canonical URL), robots.txt, depth/limit, and link discovery via the already-present `render("links")` (`document_core.py:284`). Replace the 501 (`service.py:122`) with a streaming job. Adopt Crawlee's adaptive HTTP↔browser fallback on the `resolve(browser=...)` seam.
8. **Write user docs.** A README + getting-started + API reference generated from the same stubs. Pick **one** primary evaluation trigger (`.collect()` *or* `wc.execute()`), document the other as an alias, and resolve the `.text` vs `.attr("text")` split (`document_core.py:364`).
9. **Enrich markdown** (tables, nested lists, code fences) to close the gap with Firecrawl (`document_core.py:77-100`).

---

## 5. "What to build next" roadmap (end-user value)

1. **Milestone A — Trustworthy fetch (P0 + #4/#5).** Real streaming, retries/backoff, rate limiting, proxy rotation, bounded server. *Outcome: it can hit real sites without leaking or giving up.*
2. **Milestone B — LLM-ready extraction (#6 + #9).** `project(Model)` → typed rows / DataFrame; richer markdown; `summary(url)` hardened. *Outcome: "URL → clean structured data" in one chain — the Firecrawl value, self-hosted, in one API that also runs remote.*
3. **Milestone C — Crawl (#7).** Frontier + robots + adaptive HTTP/browser + streaming crawl over the service. *Outcome: "crawl this site → stream markdown/JSON" — the headline capability.*
4. **Milestone D — Evasion (stealth backing).** Fingerprint/stealth + CAPTCHA hooks as an opt-in backing bound at session creation. *Outcome: survives 2026-era anti-bot on the pages that matter.*
5. **Milestone E — Product polish.** Docs, one trigger, DataFrame/dataset sinks, per-host budgets, observability on the existing event bus.

**Bottom line for the author:** the *foundation* is real and, in its factoring, better than the incumbents — protect the single-mechanism plan IR at all costs. But "a solid foundation" should mean *the resilience and streaming primitives are trustworthy*, and today two of them (streaming, retries) are not. Fix P0/P1 before adding features; they are the difference between an elegant demo and a foundation others can build on. The LOC "budget" in `PLAN.md` is the wrong north star — usefulness (crawl, extract, evade, stream) is.
