# Competitor Analysis & Feature-Gap Investigation: `webclient`

*Subject:* `/home/zeus/git/web-client` @ `refactor/async-core-backends` (b39a501)
*Scope:* Where does this declarative web client / browser-as-a-service stand versus the 2026 market, and what **whole features** are missing — optimizing for usefulness to LLM/agents and end users.
*Method:* Full read of the current surface (`webclient/__init__.py`, `service.py`, `tools.py`, the `core/` backings) plus web research of shipped competitor capabilities. Builds on `docs/assessment.md` and `docs/llm-usability.md`, both ~6 months stale on the code (they predate crawl, search, the summary facets, task verbs, `project(model)`, and the resiliency-policy models — all now present). **No code was changed.**

---

## 0. What we ACTUALLY have today (ground truth from the code)

This is the honest inventory the rest of the doc is graded against. "Present" means wired and reachable; "model-only" means the data model exists but no behaviour is attached yet.

**Fetch & parse — present.**
- `fetch` / `ref.resolve()`; HTML tree ops `select` / `select_all` / `attr` / `text_content` / `title` over lxml (css + xpath); JSON dotted-path `select` / `select_all` / `attr` (`core/document/html.py`, `json.py`).
- Element-as-document nesting; error policy RAISE/RETURN, contextvar-scoped so extract/filter degrade per-field.

**LLM-ready rendering — present but thin.** `render("markdown" | "text" | "elements" | "links")`; `text` strips nav/chrome via `main_content_only`. Markdown covers headings/p/lists(one level)/pre/blockquote/img but **not tables, nested lists, or definition lists** (`core/document/html.py:_md_blocks`).

**Summary facets — present and genuinely good.** `doc.summary(*include, exclude=...)` assembles `transport` (status/redirect/CDN/timing/headers), `metadata` (title/description/canonical/JSON-LD `@type`/OG keys/feeds), `structure` (toc, word count, reading time, forms + field names, link counts, pagination, media counts), `runtime` (SPA/framework detection, XHR endpoints, dynamic elements — browser-only), and `probe` (what the fetch had to escalate to). This is a strong, token-lean "what is this page" primitive competitors mostly don't expose (`core/document/summary.py`).

**Task verbs (Mode 3) — partially present.** `webclient/tools.py` ships `fetch_markdown`, `fetch_text`, `links`, `extract(url, result, fields)` (selector-map → rows). **Python-only**: there is *no* corresponding HTTP `/scrape` `/extract` `/search` task endpoint, and no `web_search` / `web_crawl` / `web_screenshot` verb.

**Typed / schema extraction — partially present.** `Collection.project(model)` validates rows into a pydantic model (typed rows) — the selector-driven half of schema extraction (`collection.py:234`). There is **no** natural-language / prompt-guided extraction (Firecrawl's `extract` takes a JSON schema + prompt and infers fields; we require the caller to supply selectors).

**Search — present but weak.** `client.search(query, limit, endpoint)` scrapes **DuckDuckGo's HTML endpoint** and returns structured `SearchResult` hits (rank/title/url/description) (`core/client/search.py`). Single provider, no answer synthesis, no citations/RAG shaping, no `site:` param at the verb, and it depends on a fragile third-party HTML layout.

**Crawl + sitemap — present.** Real BFS/best-first crawl with a deduped frontier, robots.txt obeyance, same-origin scoping, `include`/`exclude` path filters, keyword best-first ranking, and depth/width/max_pages bounds; `sitemap` is the eager single-domain variant. Exposed over HTTP at `/crawl` and `/sitemap`, runnable on a named session (crawl behind a login) (`core/crawl/`, `service.py`). Output is one `Summary` per page + the unresolved frontier — LLM-efficient.

**Live browser — present.** Playwright `click` / `write` / `wait_for` / `select` / `evaluate` / `screenshot`; console + DOM-mutation + XHR/fetch capture onto the document; **action-chain replay via `reload()`** (the recorded `actions` list re-drives the page) (`core/document/live.py`). `screenshot` returns a binary Document (PNG).

**Sessions / remote / service — present.** Cookie sessions (client + server-side); a `RemoteWebClientCore` that is the *same cores in a third dispatch mode* (plan IR POSTed to `/execute`); a FastAPI service with `/execute`, `/document`, `/sessions`, `/crawl`, `/sitemap`, and a `/events` websocket. **Structured, agent-actionable errors** everywhere (`{type, message, status_code, retriable, hint}`), a bearer-token gate, an **SSRF guard** (`block_private_hosts`), an LRU doc store, and session sweeping (`service.py`). This is a real, safe-ish self-host tier.

**Events — present.** Typed event bus (Navigation/Network/DOM/Action/Console/Plan events), streamed over the `/events` websocket.

**Resiliency policies — MODEL-ONLY (P0).** `RetryPolicy`, `RatePolicy`, `ProxyPolicy`, `AntiBotPolicy`, `BrowserPolicy`, bundled in `Resolve`, with an `AUTO` escalate-on-evidence sentinel (`core/reference/models.py`). The module docstring is explicit: *"P0: the models; behaviour lands in later phases."* The memory log confirms "policy models + the probe summary facet (**no behaviour change**)." So today: retry/backoff exists at the transport (the `RetryPolicy` generalises the client's existing `retries`/`retry_backoff`); **rate limiting, proxy rotation, anti-bot/stealth, and adaptive HTTP↔browser are declarable but not enforced.**

**The structural differentiator — present and real.** One recorder → one serializable Plan IR → one executor, reused verbatim for eager / lazy / async / remote / service. A feature written once (search, crawl) works in all modes and serializes to the wire for free. No competitor has this factoring.

---

## 1. Market map

The field has bifurcated into **LLM-data APIs** (managed SaaS that turn a URL/query into clean tokens) and **automation/infra** (browsers, crawlers, unblockers you drive). We sit oddly across both: a self-hostable library with the *primitives* of the SaaS layer and the *extensibility* of the infra layer.

| Player | Best at | Shape |
|---|---|---|
| **Firecrawl** | The reference "LLM-ready web" API: `scrape`→markdown, `crawl`, `map`, `search` (search+scrape in one call), `extract` (JSON-schema + NL prompt across many URLs), page **actions**, **change-tracking**, batch, **webhooks**, the **FIRE-1** agent, and a first-party **MCP server**. | SaaS + limited OSS self-host |
| **Tavily** | Search built for RAG: `/search` (+`include_raw_content`), `/extract`, `/crawl`, `/map`, `/research`; citation-ready, source-authority-weighted results for agents. | SaaS |
| **Crawlee (Python)** | Crawling engine: request queue, dedup, **AutoscaledPool** (CPU/mem/event-loop-lag driven), **AdaptivePlaywrightCrawler** (HTTP-vs-browser decided per request), politeness, session/proxy rotation. | OSS library |
| **Browserbase / Browserless** | Managed headful browsers over Playwright/Puppeteer/**CDP**; **stealth**, **auto-CAPTCHA**, residential proxies, fingerprint randomization, saved auth **Contexts**, session record/replay; Browserless self-hosts and ships an **MCP server** + BrowserQL. | SaaS (Browserless also self-host) |
| **Scrapy / Apify** | Mature crawling framework (Scrapy) + the **Actor**/dataset/scheduling platform (Apify) with a large store of pre-built scrapers. | OSS + PaaS |
| **ScrapingBee / ScraperAPI / Zyte / Oxylabs / Bright Data** | Proxy + **unblock** APIs: rotating residential/ISP pools, anti-bot bypass, JS render, geo-targeting — the "just get me the HTML" layer. | SaaS |
| **Jina Reader (`r.jina.ai`)** | Dead-simple URL→clean-markdown (prefix the URL); PDF support; the zero-friction LLM-readability baseline. (Jina acquired by Elastic, Oct 2025.) | SaaS (+ OSS reader) |
| **Exa** | Neural/semantic **search** + content retrieval for LLMs; discovery over parsing. | SaaS |
| **Diffbot** | Vision+NLP **automatic** structured extraction (article/product/etc.) and a **Knowledge Graph** with entity extraction — no selectors needed. | SaaS |

**Axes that matter (and who owns them):**
1. **LLM-readiness** (URL→markdown/structured, token-lean) — Firecrawl, Jina, Tavily.
2. **Crawl** (frontier/dedup/robots/scope) — Crawlee, Scrapy, Firecrawl.
3. **Extraction / schema** (NL+JSON-schema, auto-entity) — Firecrawl, Diffbot.
4. **Anti-bot / proxy / unblock** — Zyte/Oxylabs/Bright Data, Browserbase/Browserless.
5. **Browser / actions** (click/type/wait, stealth, CAPTCHA, CDP) — Browserbase/Browserless.
6. **Search** (web + RAG/citations) — Tavily, Exa, Firecrawl.
7. **Change-tracking / monitoring** — Firecrawl (leading).
8. **Batch / async jobs + webhooks** — Firecrawl, Apify.
9. **MCP / agent-native** — Firecrawl, Browserless, most infra players now ship one.
10. **Self-host** — Crawlee, Scrapy, Browserless (Docker), **us**.

---

## 2. Feature matrix

Grounded in §0 for "Us." ✅ = shipped/wired · ◐ = partial / model-only / thin · ✗ = absent. Competitor columns reflect shipped 2026 capability.

| Capability | **Us** | Firecrawl | Tavily | Crawlee | Browserbase/less | Jina/Exa | Zyte/BrightData | Scrapy/Apify |
|---|---|---|---|---|---|---|---|---|
| URL → LLM-ready markdown | ◐ no tables | ✅ best-in-class | ✅ | ✗ | ◐ | ✅ | ◐ | ✗ |
| Structured extract (selectors) | ✅ | ✅ | ◐ | ✅ | ◐ | ✗ | ✗ | ✅ |
| Typed rows (pydantic/schema) | ✅ `project(model)` | ✅ | ◐ | ◐ | ✗ | ✗ | ✗ | ◐ Items |
| **NL + JSON-schema extract** | ✗ | ✅ `extract` | ◐ | ✗ | ✗ | ✗ | ✗ | ✗ |
| Auto entity extraction | ✗ | ◐ | ✗ | ✗ | ✗ | ✗ | ✗ | Diffbot✅ |
| Web search | ◐ DDG scrape | ✅ | ✅ | ✗ | ◐ Fetch API | ✅ Exa | ✗ | ◐ |
| Search+RAG/citations | ✗ | ◐ | ✅ | ✗ | ✗ | ✅ | ✗ | ✗ |
| Map / sitemap | ✅ | ✅ | ✅ | ◐ | ✗ | ✗ | ✗ | ◐ |
| Crawl (frontier/dedup/robots) | ✅ | ✅ | ✅ | ✅ core | ◐ | ✗ | ✗ | ✅ core |
| Browser automation (click/type/wait) | ✅ | ◐ actions | ✗ | ✅ | ✅ | ✗ | ◐ | ◐ |
| Screenshot output | ✅ | ✅ | ✗ | ✅ | ✅ | ✗ | ◐ | ◐ |
| PDF output / PDF parsing | ✗ | ✅ | ◐ | ✗ | ✅ | ✅ Jina | ✗ | ◐ |
| **Adaptive HTTP↔browser (auto)** | ◐ model-only | ✅ | ✅ | ✅ | ✗ | ✅ | ✅ | ◐ |
| Anti-bot / stealth | ◐ model-only | ✅ | ◐ | ◐ | ✅ | ✗ | ✅ | ◐ |
| Proxy rotation / pool | ◐ model-only | ✅ | ✅ | ✅ | ✅ | ✗ | ✅ core | ◐ |
| CAPTCHA solving | ✗ | ✅ | ✗ | ✗ | ✅ | ✗ | ✅ | ✗ |
| Rate limiting / politeness | ◐ model-only | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Retry / backoff | ◐ transport | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| **Change-tracking / monitoring** | ✗ | ✅ | ✗ | ✗ | ✗ | ✗ | ✗ | ◐ (Apify) |
| **Batch / async jobs** | ✗ sync-only | ✅ | ◐ | ✅ | ✅ | ◐ | ✅ | ✅ |
| **Webhooks** | ✗ | ✅ | ✗ | ◐ | ✅ | ✗ | ◐ | ✅ |
| Sessions / cookies / auth state | ✅ | ◐ | ✗ | ✅ | ✅ Contexts | ✗ | ◐ | ✅ |
| Self-host | ✅ | ◐ OSS | ✗ | ✅ | ◐ Browserless | ✗ | ✗ | ✅ |
| **One IR: sync=async=remote=lazy** | ✅ unique | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ |
| **MCP server / agent-native tools** | ✗ (Py verbs only) | ✅ | ◐ | ✗ | ✅ Browserless | ◐ | ◐ | ◐ |
| Structured/actionable errors | ✅ | ✅ | ✅ | ◐ | ✅ | ◐ | ◐ | ◐ |
| Typed IDE surface (stubs) | ✅ | N/A SaaS | N/A | ✅ | N/A | N/A | N/A | ◐ |

**Reading of the matrix.** We are competitive-to-leading on the *structural* rows (one-IR, self-host, sessions, structured errors, summary facets, typed surface) and on *crawl + browser automation*. We are behind on the *commercial value* rows that LLM buyers actually pay for: NL-schema extract, real anti-bot/proxy, change-tracking, async jobs+webhooks, and an MCP server. Crucially, several rows are ◐ *only because the behaviour is unwired* (`Resolve` policies) — the design already exists, which lowers their effort.

---

## 3. Gap analysis — whole features we're missing or thin on

Ranked by **value to LLM/agents × (inverse) effort**, given the cores+backings kernel. "Effort" assumes we keep the structure and add backings/endpoints (re-wiring OK, rewrite not).

### Tier 1 — highest leverage, mostly assembly over what exists

1. **MCP server + HTTP task-verb endpoints.** *(value: very high; effort: low–med.)* The Python task verbs exist (`tools.py`) but there is no MCP server and no `/scrape` `/extract` `/search` `/crawl` `/screenshot` HTTP endpoints — the exact shape Firecrawl/Browserless ship and the #1 way agents consume this category. The service, structured errors, and rendering primitives are already there; this is an adapter, not new capability. **Biggest single win for "useful to LLMs."**

2. **NL + JSON-schema extraction (`extract` with a prompt).** *(value: very high; effort: med.)* We have selector-driven typed rows (`project(model)`) — the deterministic half. Missing is Firecrawl's headline: give a JSON schema + a natural-language description, get structured objects *without* authoring selectors (LLM infers them). Fits as a new backing/verb that runs an LLM over `render("text"/"elements")` and validates into the pydantic model we already accept. This is the feature LLM/RAG users reach for first.

3. **Wire the resiliency policies (rate limit, proxy rotation, adaptive browser).** *(value: high; effort: med — design is done.)* `RatePolicy`, `ProxyPolicy`, `BrowserPolicy` are model-only. Wiring them into the transport ladder makes crawl/search survive real sites and turns three ◐ cells into ✅. `BrowserPolicy(when="auto")` is the Crawlee adaptive HTTP↔browser rule — high value because it makes "just fetch it" work on JS-gated pages automatically. **The models being pre-designed is the reason this is Tier 1 not Tier 2.**

4. **Batch / async jobs + webhooks.** *(value: high; effort: med.)* Crawl is synchronous and blocking (`/crawl` runs to completion in the request). Agents and pipelines want "start a crawl/batch, get a job id, poll or receive a webhook." The event bus + `/events` websocket already model progress; add a job registry (id → running crawl/batch) + webhook POST-on-completion. Unlocks crawl-scale and long jobs without holding a connection.

### Tier 2 — real gaps, more work or narrower audience

5. **Richer markdown (tables, nested lists, code fences).** *(value: med–high; effort: low.)* Every `fetch_markdown`/`web_crawl` result an LLM reads is degraded by the missing tables/nested lists in `_md_blocks`. Cheap, directly improves output quality vs Firecrawl/Jina. (Carried over from `docs/assessment.md`, still open.)

6. **Real anti-bot / stealth + CAPTCHA.** *(value: high for the hard 20% of sites; effort: high.)* `AntiBotPolicy` is model-only; there is no fingerprint/stealth and no CAPTCHA path. This is where Browserbase/Browserless/Zyte win. A stealth backing bound at session creation is the right seam; CAPTCHA likely means integrating a third-party solver. High value but high effort and partly outside a pure-Python library's reach.

7. **Change-tracking / monitoring.** *(value: high for agent workflows; effort: med.)* Firecrawl's differentiator: content-hash a page, re-scrape only on change, emit a diff. We have the primitives (summary/transport already compute size/hash-able content; the event bus can carry a `ChangeEvent`). A `monitor(url, interval)` job + content-hash store would be a genuine, defensible feature — and it composes with #4 (jobs) and the plan IR (a stored plan re-run on a schedule).

8. **PDF as output and as input.** *(value: med; effort: med.)* No `render("pdf")` and no PDF *parsing* (a fetched PDF isn't turned into text/markdown). Both are table stakes for Jina/Firecrawl. PDF-in (parse to text) is the higher-value half for RAG.

9. **Search: multi-provider + RAG shaping.** *(value: med–high; effort: med.)* Today it scrapes DuckDuckGo HTML — fragile and unranked-for-RAG. Add a provider abstraction (Tavily/Brave/SerpAPI/Exa as pluggable backings — a natural fit for the backing kernel), a `site:` param, and optional answer/citation shaping for RAG.

### Tier 3 — polish / niche

10. **Managed auth/login flows.** We have sessions+cookies+action-replay (the raw material) but no first-class "log in and keep the context" helper (Browserbase Contexts). Med value, med effort.
11. **Datasets / export sinks.** No dataset/CSV/Parquet/DataFrame sink (Apify-style). `project()` returns dicts; a `.to_dataframe()`/sink is low effort, useful for the data-eng audience.
12. **Global concurrency budget / autoscaling.** Crawlee's AutoscaledPool has no analog; per-host caps + a global budget would harden crawl-scale.

---

## 4. Positioning & priorities

**Our real, defensible moat is structural, not feature-parity:** *one plan IR that is simultaneously the eager path, the lazy path, the wire format, and the remote/service path* — self-hostable, with a cores+backings kernel where a whole feature (search, crawl) is one backing built on the interface. No competitor has this. Everything below protects and monetizes that, without rewriting it.

**Where to double down (lean into, don't chase parity):**
- **Self-hostable, agent-native, one-API.** The pitch is "Firecrawl's tool surface, but self-hosted, typed, and it runs sync/async/remote/lazy from one definition, and you can add a capability as a backing." That's a real segment (compliance/data-control buyers, per Browserless's own positioning) no SaaS serves.
- **The summary facets.** `doc.summary()` is a token-lean "understand this page" primitive competitors don't expose. Make it a headline agent verb (`web_summary`), not just an internal.
- **Plan IR as audit/replay/schedule substrate.** A stored plan re-run on a schedule *is* change-tracking and monitoring for free — a differentiated feature that falls out of the architecture.

**Highest-leverage roadmap (each step is backings/endpoints over the existing kernel):**

- **M1 — Be agent-consumable (Tier-1 #1).** Ship an **MCP server** + HTTP task-verb endpoints (`/scrape`, `/extract`, `/search`, `/crawl`, `/map`, `/screenshot`, `/summary`) wrapping `tools.py` + the summary/crawl/search we already have, with structured output schemas and truncation/pagination. *Outcome: an agent can actually use us today; we enter the MCP-server conversation.* Lowest effort, highest visibility.
- **M2 — Be trustworthy on real sites (Tier-1 #3).** Wire `RatePolicy` / `ProxyPolicy` / `BrowserPolicy(when="auto")` into the transport ladder (design already done in `core/reference/models.py`). *Outcome: crawl/search survive JS-gated and rate-limiting sites; three ◐ cells become ✅; the `probe` facet starts reporting real escalations.*
- **M3 — Be an extraction product (Tier-1 #2 + Tier-2 #5).** NL+JSON-schema `extract` (LLM over `render` → validated `project(model)`), plus richer markdown (tables/nested lists). *Outcome: the Firecrawl value — "URL → typed data / clean markdown in one call" — self-hosted.*
- **M4 — Be a job platform (Tier-1 #4 + Tier-2 #7).** Async jobs + webhooks over the event bus; then **change-tracking/monitoring** as a scheduled stored-plan + content-hash — a feature the architecture uniquely makes cheap. *Outcome: crawl-scale + monitoring, our differentiated angle.*
- **M5 — Harden the hard 20% (Tier-2 #6, #9; Tier-3).** Stealth backing + CAPTCHA/solver integration; multi-provider search; PDF in/out; datasets. *Outcome: closes the anti-bot and search-quality gaps for the pages that matter.*

**Keep the kernel; the whole plan is additive.** M1–M4 are new backings, new value models, and thin service/MCP adapters — exactly what the cores+backings structure is for. The only re-wiring is threading the (already-designed) `Resolve` policies through the transport. No big rewrite is warranted or recommended.

---

## 5. Sources

- [Firecrawl — product overview (scrape/search/crawl/map/extract)](https://www.firecrawl.dev/)
- [Firecrawl — search endpoint (search + extract in one call)](https://www.firecrawl.dev/blog/mastering-firecrawl-search-endpoint)
- [Firecrawl — crawl endpoint guide](https://www.firecrawl.dev/blog/mastering-the-crawl-endpoint-in-firecrawl)
- [Firecrawl API tutorial: scrape, crawl, map — 2026 (Apify)](https://use-apify.com/blog/firecrawl-api-tutorial)
- [Firecrawl — best web search APIs for AI (2026)](https://www.firecrawl.dev/blog/best-web-search-apis)
- [Firecrawl official MCP server (scrape/crawl/search/map/extract)](https://github.com/firecrawl/firecrawl-mcp-server)
- [Tavily Docs — Crawl & content extraction](https://docs.tavily.com/examples/quick-tutorials/crawl-api)
- [Tavily review 2026 — features & pricing](https://aiagentslist.com/agents/tavily)
- [Tavily — search API for AI agents & RAG (Agents Index)](https://agentsindex.ai/tavily)
- [Crawlee for Python — scaling crawlers (AutoscaledPool)](https://crawlee.dev/python/docs/guides/scaling-crawlers)
- [Crawlee for Python — AdaptivePlaywrightCrawler](https://crawlee.dev/python/docs/guides/adaptive-playwright-crawler)
- [Browserless vs Browserbase — headless browser infra](https://www.browserless.io/blog/browserless-vs-browserbase)
- [Browserbase vs Browserless vs Hyperbrowser vs Anchor (2026)](https://mcp.directory/blog/browserbase-vs-browserless-vs-hyperbrowser-vs-anchor-2026)
- [Best MCP servers for browser automation (2026)](https://www.webfuse.com/blog/the-top-5-best-mcp-servers-for-ai-agent-browser-automation)
- [Jina AI Reader (r.jina.ai) — URL → LLM-ready markdown](https://jina.ai/reader/)
- [Jina Reader vs Diffbot — LLM-ready extraction](https://datascientist.fr/en/blog/jina-reader-vs-diffbot)
- [Jina Reader alternatives — 7 AI scraping tools (Exa/Diffbot context)](https://scrapegraphai.com/blog/jina-alternatives)
- [Firecrawl/Tavily alternatives overview (Bright Data)](https://brightdata.com/blog/ai/firecrawl-alternatives)
- [Anthropic — Writing effective tools for AI agents](https://www.anthropic.com/engineering/writing-tools-for-agents)
- [MCP — Tools (2025-06-18 spec: structured output)](https://modelcontextprotocol.io/specification/2025-06-18/server/tools)
- Internal: `docs/assessment.md`, `docs/llm-usability.md` (prior analyses; ~6 months stale on the code).
