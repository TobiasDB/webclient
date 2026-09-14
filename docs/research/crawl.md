# Crawl: a best-in-class case study

*Status: research / case study. No code proposed as committed — this is the "how far
could we push it" study for the crawl feature.*
*Scope: `webclient/core/crawl/` (the `Crawl` core + `CrawlBacking`), the `crawl`/`sitemap`
client verbs (`core/client/__init__.py`), and the `/crawl`,`/sitemap` service endpoints
(`service.py`). Optimising for usefulness to LLMs/agents and to end users.*
*Companions: `docs/assessment.md` §2.2/§3 (crawl gap), `docs/llm-usability.md` §6.1
(`web_crawl` tool shape), `docs/design/resiliency.md` (the ladder the crawl should ride).
This builds on those; it does not repeat them.*

---

## 1. State of the art

The mature crawlers converge on the same machine — a **scheduler over a deduplicated
frontier, with pluggable politeness/fetch policy, streaming output, and (increasingly)
LLM-shaped results** — and differ mostly in ergonomics.

**Scrapy** — the reference architecture. A **Scheduler** pops from disk/memory priority
queues; the **`RFPDupeFilter`** fingerprints each request (canonicalised method+URL+body)
so the same page is never scheduled twice; **middlewares** are the extension seam
(`RetryMiddleware`, `RobotsTxtMiddleware`, `HttpCacheMiddleware`, `OffsiteMiddleware`,
`AutoThrottle`). **`AutoThrottle`** sets per-host delay from observed latency
(`delay = latency / target_concurrency`, and never *lowers* delay on non-200). `scrapy-deltafetch`
skips URLs seen in a prior run (incremental). Priorities are integer `Request.priority`; a
`DEPTH_PRIORITY` knob turns the frontier from DFS→BFS→best-first.

**Crawlee for Python** — the modern shape. A persistent **`RequestQueue`** (resumable
across process restarts) is the frontier; **`enqueue_links(strategy=...)`** offers
`all` / `same-domain` / `same-hostname` / `same-origin` scoping plus include/exclude
**glob & regex** globs on the URL; a **`SessionPool`** rotates identities and retires a
session on 401/403/429, with `max_session_rotations` kept as a **separate budget** from
`max_request_retries`. **`AdaptivePlaywrightCrawler`** runs a *rendering-type predictor*:
it tries cheap HTTP, compares extracted output against a browser render, learns per-URL
which is needed, and periodically re-checks — so it only pays for a browser when the page
is JS-gated. Autoscaling sizes concurrency to CPU/memory + latency.

**Firecrawl (v2 crawl endpoint)** — the LLM-native benchmark. `POST /v2/crawl` starts an
**async job** (poll a job id, or receive **webhooks**: `crawl.page` / `crawl.completed` /
`crawl.failed`, each signed with an `X-Firecrawl-Signature` HMAC-SHA256). Steering is
declarative: **`includePaths`/`excludePaths`** (regex arrays over the path),
**`maxDiscoveryDepth`** (depth in *discovery order*; the seed and sitemapped pages are
depth 0), **`limit`**, **`sitemap: skip|include|only`** (sitemap-as-seed is first-class,
not an afterthought), and a natural-language **`prompt`** that the service compiles into
include/exclude rules. Every page comes back as **LLM-ready markdown** (main-content
detection, chrome stripped, repeated blocks de-duplicated) plus optional schema-extracted
JSON. **Change tracking**: a `changeTracking` format stores a snapshot per URL and returns
`new|same|changed|removed` with a diff — the basis for **incremental crawling** (re-fetch
only what changed, via stored checksums / ETag / Last-Modified).

**Browserless / Apify** — the infra tier. Proxy/region bound **sticky-by-default at session
creation** with opt-out rotation; stealth + CAPTCHA solving as managed capability;
load-balanced, autoscaled browser pools. Apify's platform adds durable request queues +
datasets so a crawl is a resumable, inspectable **job**, not a function call.

**The cross-cutting techniques worth naming:**
- **URL canonicalisation before the frontier** (lowercase host, drop default port, strip
  fragment, sort/strip tracking query params, normalise trailing slash) — catches
  URL-variant duplicates *before* fetching. **Content hashing after** catches identical
  bodies at different URLs. **Near-duplicate** (SimHash/MinHash) filtering avoids redundant
  RAG chunks.
- **Sitemap + `robots.txt` seeding** (read `Sitemap:` directives and `/sitemap.xml`) to
  discover URLs breadth-first without link-walking, and **`Crawl-delay`** honouring.
- **Frontier strategy**: BFS (shallow-first) is table-stakes; **best-first / focused
  crawling** (score edges by a relevance signal — anchor text, URL tokens, parent-page
  context; classically BM25 or a learned classifier) is what makes a *bounded* crawl land
  on the right pages. A bounded priority queue (heap) with a frontier cap keeps it scalable.
- **Incremental / streaming results** (yield each page as it completes; cursor/webhook for
  continuation) so an agent or user sees output before the whole crawl finishes.
- **Resumability / checkpointing**: the frontier + seen-set persist, so a crawl is a job
  that survives restarts and can be paused/resumed.

---

## 2. Our current implementation — an honest assessment

The crawl is a **stateful, client-held, turn-based frontier crawler** — a genuinely
distinctive shape. `wc.crawl(seeds, ...)` returns a `Crawl` core (`core/crawl/__init__.py`)
used as a context manager; the client owns the frontier (dedup / scope / fetching) and
**defers round selection to the caller**, or self-drives when `auto=True`. `CrawlBacking`
(`core/crawl/backing.py`) is one backing over the existing cores — it reuses
`client.afetch` for transport and `doc.select_all("a[href]")` for link discovery. That is
the right factoring, and the turn-based/auto split is the one thing here that is *ahead* of
the incumbents for agent use (see §3).

**What we actually have, concretely:**
- **Frontier + scheduling** (`_select`/`_score`): auto mode re-sorts the whole frontier each
  round and takes the top-`width` edges; `_score` is naive term-frequency — `sum(blob.count(k)
  for k in keywords)` over anchor-text+URL, minus a tiny depth tie-break. With no keywords it
  degrades to shallowest-first (BFS). Turn-based mode filters the frontier by the caller's
  picked edges/URLs.
- **Scope + filters** (`_expand`): `same_origin` compares `hostname == scope`; `include`/
  `exclude` are **single substring** tests on the path.
- **Dedup** (`_seen`): a `set[str]` of raw URLs with only the `#fragment` stripped.
- **Robots** (`_allowed`/`_load_robots`): one `RobotFileParser`, loaded lazily from the
  **first** sampled URL's host and cached on the core.
- **Sitemap**: `wc.sitemap(url)` is an **eager `auto` crawl run to completion** — a BFS
  link-walk, *not* a `/sitemap.xml` reader.
- **Output**: `core.pages.append(doc.summary())` — a `Summary` per page; the service returns
  `{pages, urls, frontier, done}`.
- **Bounds**: `done` = closed ∨ empty frontier ∨ `len(pages) >= max_pages`; `max_depth`
  caps link depth.
- **Transport funnel**: every fetch goes through `client.afetch`, so the crawl already
  inherits `retries`/`retry_backoff`, `min_interval` politeness (`_pace`), and the
  `block_private_hosts` SSRF guard for free.
- **Tests** (`tests/test_crawl.py`): same-origin, robots on/off, turn-based, best-first
  keyword, anchor text retained, sitemap, context-manager close. Real coverage of the
  mechanism.

**The gaps — honestly:**

1. **Output has no page content.** `doc.summary()` returns only the five facets
   (transport/metadata/structure/runtime/probe — confirmed in `core/document/summary.py`);
   **there is no markdown or text**. An agent that crawls a docs site gets per-page
   *metadata* and never the readable content. This is the single biggest usefulness gap: the
   headline "crawl → LLM-ready markdown per page" (Firecrawl's whole value, and our own
   `web_crawl` sketch in `docs/llm-usability.md` §6.1) is not delivered.
2. **Dedup is URL-variant blind.** No canonicalisation: `/a`, `/a/`, `/a?utm_source=x`, and
   `HTTP://Host/A` are four different frontier entries → duplicate fetches and re-summaries.
   No content-hash dedup, so the same body at two URLs is fetched and summarised twice.
3. **`sitemap()` doesn't read sitemaps.** It ignores `/sitemap.xml`, the `robots.txt`
   `Sitemap:` directive, and even `Metadata.sitemap_url`/`feeds`, which the summary layer
   *already extracts*. It is a misnamed BFS.
4. **Robots is single-host and shallow.** One parser for the first host is wrong for a
   multi-host crawl (`same_origin=False`); `Crawl-delay` is not honoured; no per-host cache;
   `robots.txt` failures silently allow everything.
5. **No concurrency.** `step` awaits `afetch` in a sequential `for` loop — one page at a
   time. The engine has a bounded pool and a true-streaming `fan_out_stream`
   (`query/executor.py`), but the crawl uses none of it.
6. **No streaming / job / cursor.** `run()` blocks to completion; the service returns the
   whole result in one response. No incremental output, no job id, no webhook, no
   continuation cursor — the opposite of Firecrawl's async-job model.
7. **Not resumable.** `_seen` and `_robots` are `PrivateAttr` (not serialised). `ICrawl`
   itself is a pydantic model (frontier+pages serialise), but a rehydrated crawl **loses its
   dedup ledger**, so it cannot be checkpointed/resumed correctly.
8. **Filters are weak.** `include`/`exclude` are one substring each on the path — no lists,
   no globs, no regex, no full-URL matching (cf. Firecrawl `includePaths[]`/`excludePaths[]`).
9. **Scheduling is naive and unbounded.** Term-frequency scoring (no BM25, no IDF, no
   parent-context), a full O(n log n) re-sort each round, and an **uncapped frontier** (a
   large site grows `frontier` without limit).
10. **No adaptivity or change-detection.** No HTTP↔browser escalation for JS-gated pages
    (the resiliency ladder is designed but not wired here), and no conditional-request /
    content-diff incremental crawl.
11. **Crawl isn't a plan.** It's imperative Python in a backing calling `afetch` directly.
    Fine for local + the service (which runs it server-side), but `remote_client.crawl()`
    won't round-trip as a single op the way a recorded plan would.

---

## 3. Best-in-class target

A fully-realised crawl here, prioritised — and split by **what is genuinely valuable to an
LLM/agent** vs **table-stakes every crawler must have**.

### Genuinely valuable to LLMs/agents (this is where to invest — it's our differentiator)

- **The steerable turn-based frontier — keep and sharpen it.** `crawl.step(picks)` letting
  an agent choose each round is a capability Firecrawl/Crawlee *don't* expose; it is exactly
  the "agent steers a crawl" ergonomic. Make the per-round surface **token-lean**: return a
  compact frontier view (top-N edges with their score and *why* — anchor text + matched
  keyword), and a per-round **delta** (what was just fetched), so steering costs few tokens.
- **Best-first / focused crawling toward a goal.** Auto mode's keyword steer is the right
  idea; upgrade the signal (BM25-lite over anchor+URL+parent context) and add a
  **`stop_when` goal predicate over `Summary`** (the roadmap's own lean) so a crawl ends when
  it has found what the agent asked for, not only when bounds are hit. Focused crawling on a
  bounded budget is the highest-leverage agent feature.
- **One-shot page payload: markdown + probe + (optional) schema rows.** Each crawled page
  should carry LLM-ready **markdown** *and* its `probe` (was-browser/anti-bot) and optional
  schema-extracted fields — so "crawl this site → structured, readable pages" is one call.
- **Resumable jobs with cursors.** An agent works across turns; a crawl should be a **job**
  it can start, page through with an opaque cursor, and resume — not a blocking call whose
  result must fit one response.
- **Change tracking for "watch this".** `new|same|changed|removed` per page (Firecrawl
  `changeTracking`) turns the crawler into a monitor — a distinctly agent-useful verb.

### Table-stakes (must-have to be credible, but not differentiating)

- **URL canonicalisation + content-hash dedup** before/after the frontier.
- **Real sitemap + robots seeding**, `Crawl-delay` honouring, per-host robots.
- **Regex/glob `includePaths[]`/`excludePaths[]`, `maxDiscoveryDepth`, `limit`.**
- **Bounded concurrency** within a round + **per-host rate limiting** (AutoThrottle-style).
- **Incremental streaming output** (yield each page as fetched) + a bounded, capped frontier
  (priority heap).
- **Adaptive HTTP↔browser** per page (ride the resiliency ladder; record `probe`).
- **Session/proxy rotation** for large or defended crawls (the resiliency `ProxyService`).

---

## 4. Concrete recommendations for THIS codebase

Structure stays: **cores + backings + dispatch**, `afetch` as the single transport funnel,
`Summary` as the page atom, remote as a core-swap. Every item below is a re-wiring, not a
rewrite, and names its seam. Ordered by leverage.

### P0 — make the crawl actually useful and its dedup correct (small, high-value)

1. **Put page content in the output.** Add a `content`/`markdown` facet to `Summary`
   (`core/document/models.py` + a small backing in `core/document/summary.py`) *or* let the
   crawl store `(summary, render("markdown"))`. Give `crawl(...)` a Firecrawl-shaped
   **`formats`/`include`** param (`markdown` | `summary` | `links`) controlling per-page
   payload. **Seam:** `CrawlBacking.step` line `core.pages.append(doc.summary())`; `ICrawl`
   adds the format option; `_crawl_response` in `service.py` already dumps whatever `pages`
   holds. *This is the one change that makes `web_crawl` deliver its promise.*

2. **Canonicalise URLs for dedup.** Add `_canon(url)` in `core/crawl/backing.py` (lowercase
   host, drop default port, strip fragment [already], sort/strip tracking query params,
   normalise trailing slash) and route both the `_seen` seed (`Crawl.bind`) and `_expand`
   through it. **Seam:** `backing.py::_expand` + `crawl/__init__.py::bind`.

3. **Content-hash dedup.** After a successful fetch in `step`, hash `doc.content`; skip and
   don't re-summarise a body already seen. Keep a `_seen_hashes: set` alongside `_seen`.
   **Seam:** `backing.py::step`.

4. **Real sitemap seeding.** `_seed_sitemap(core, seed)` that reads the `robots.txt`
   `Sitemap:` directive (already fetching robots in `_load_robots`), `/sitemap.xml`, and
   `Metadata.sitemap_url`/`feeds` (already extracted by the summary layer), and seeds the
   frontier at depth 0. Honour a `sitemap: skip|include|only` option (Firecrawl). **Seam:**
   new helper in `backing.py`, called from `bind`/first `step`; `sitemap()` verb in
   `core/client/__init__.py` gains true semantics.

### P1 — resilient, concurrent, and correctly polite (medium)

5. **Per-host robots + `Crawl-delay`.** Key `_robots` by host in a dict; honour `Crawl-delay`
   by feeding it into `_pace`/`min_interval`. **Seam:** `backing.py::_allowed`/`_load_robots`;
   `_pace` already exists in `core/client/__init__.py`.

6. **Concurrency within a round.** Replace the sequential `for edge in chosen` loop with a
   bounded parallel fetch via the existing `fan_out_stream` (`query/executor.py`) + the
   client pool, so a round fetches `width` pages concurrently under the per-host rate cap.
   **Seam:** `backing.py::step`. This is also the first step toward…

7. **Streaming + job/cursor at the service.** Add a `step`-driven generator (`run_stream`)
   that yields each page as fetched; wire `/crawl` to return a **job id + cursor** (or SSE),
   reusing `EngineLoop.astream`. **Seam:** `CrawlBacking` (new streaming op) + `service.py`
   `/crawl` (job store — the service already has TTL'd stores from the resiliency work).

8. **Regex/list `include`/`exclude` + `maxDiscoveryDepth`.** Widen `ICrawl.include`/`exclude`
   from `str` to `list[str]` with glob/regex on the full URL; add a discovery-depth counter
   distinct from link depth. **Seam:** `core/crawl/models.py` (fields) + `backing.py::_expand`.

### P2 — best-in-class differentiators (larger, but each isolated)

9. **Better focused-crawl scoring + bounded frontier.** BM25-lite in `_score` (IDF over
   anchor/URL tokens, optional parent-page context carried on the `Edge`); replace the
   full re-sort with a `heapq` priority frontier and a **frontier cap**. Add a `stop_when`
   goal predicate over `Summary`. **Seam:** `backing.py::_score`/`_select` + `models.py`
   (`Edge` gains context; `ICrawl` gains `stop_when`/`max_frontier`).

10. **Adaptive HTTP↔browser + probe per page.** Because the crawl already funnels through
    `afetch`, wiring the resiliency `Resolve` policy (`docs/design/resiliency.md`) through
    `crawl(...)` gets adaptive rendering and anti-bot escalation *for free* once the ladder
    ships; store each page's `probe` in its `Summary`. **Seam:** thread a `resolve=` policy
    from `crawl()` into `afetch`; no new crawl machinery.

11. **Incremental / change tracking.** A crawl-scoped (or client-scoped) snapshot store keyed
    by canonical URL: send `If-None-Match`/`If-Modified-Since` (via `ref.headers`), interpret
    304, hash content, and tag each page `new|same|changed`. **Seam:** `backing.py::step` +
    a small store; surfaces as a `change` field on the page payload.

12. **Resumability.** Promote `_seen`/`_seen_hashes` (and a per-host robots snapshot) into
    serialisable `ICrawl` state so a `Crawl` can be dumped and rehydrated with its dedup
    intact — then a service crawl is a durable, resumable job. **Seam:** `core/crawl/models.py`
    (move `_seen` → a `seen: set`/`list` field) + `crawl/__init__.py::bind`.

**Sequencing note.** P0 is a few contained edits that convert the crawl from "metadata BFS"
into "LLM-ready crawl" and fix dedup honesty — do it first and in isolation. P1 makes it
scale and stream. P2 items are independent and can land in any order; #10 is nearly free once
Resiliency ships, so it should follow that feature rather than lead.

---

## 5. Sources

- Scrapy — architecture / scheduler / dupefilter / AutoThrottle:
  <https://docs.scrapy.org/en/latest/topics/architecture.html>,
  <https://docs.scrapy.org/en/latest/topics/autothrottle.html>; DeltaFetch (incremental):
  <https://github.com/scrapy-plugins/scrapy-deltafetch>
- Crawlee for Python — enqueue strategies (`all`/`same-domain`/`same-hostname`/`same-origin`),
  RequestQueue, adaptive crawler, session pool:
  <https://crawlee.dev/python/docs/introduction/adding-more-urls>,
  <https://crawlee.dev/python/docs/guides/adaptive-playwright-crawler>,
  <https://crawlee.dev/python/docs/guides/session-management>
- Firecrawl v2 crawl endpoint — `maxDiscoveryDepth`, `includePaths`/`excludePaths`, `sitemap`,
  webhooks (HMAC), async jobs:
  <https://docs.firecrawl.dev/api-reference/endpoint/crawl-post>,
  <https://www.firecrawl.dev/blog/mastering-the-crawl-endpoint-in-firecrawl>;
  change tracking / incremental crawling:
  <https://docs.firecrawl.dev/features/change-tracking>,
  <https://www.firecrawl.dev/glossary/web-crawling-apis/incremental-crawling>
- Browserless — proxies / stealth / CAPTCHA (session-bound, sticky-by-default):
  <https://docs.browserless.io/browserql/bot-detection/proxies>
- Apify — durable request queues + datasets (crawl-as-job):
  <https://docs.apify.com/platform/storage/request-queue>
- Anthropic — writing effective tools for AI agents (token-lean, actionable outputs):
  <https://www.anthropic.com/engineering/writing-tools-for-agents>
- Internal: `docs/assessment.md` (§2.2, §3), `docs/llm-usability.md` (§6.1 `web_crawl`),
  `docs/design/resiliency.md` (the ladder the crawl should ride).
