# web-* architecture

The single source of truth for the `web-*` rewrite. It is a clean-room, layered scraper built
as a **monorepo of small packages**, one per layer, each importing only the layers below it. The
current `webclient/` monolith is the reference/oracle — good ideas, but convoluted; this rebuild
takes the ideas, not the code.

> This is the only documentation for the rewrite. Keep it current; do not scatter design notes
> elsewhere.

---

## Principles

1. **Plain code first; the DSL is only a lazy face.** Every capability is ordinary async/sync
   code in its owning layer. The DSL wraps those plain methods and adds laziness, sync/async
   blocking, and remoting. If a feature can't be written as plain layer code, it's in the wrong
   layer.
2. **One package per layer; a strict acyclic import graph.** A layer imports only the layers
   below it. Each is independently installable and usable in isolation.
3. **Every layer has a well-defined boundary + interface**, expressed as a small input→output
   contract (below).
4. **Each layer defines its own event types.** The kernel holds only the `Event` base + the bus.
5. **A failure is data.** A `WebError` travels on results (a not-ok `Snapshot`) and is raised as
   `WebException` only when a layer chooses to fail loudly.

---

## The stack

```
web.dsl       lazy engine, 4 dispatch modes (sync / async / lazy / API)   ← all below
web.crawl     Goal → Documents                                            ← …, resolve
web.resolve   Request → Document  · middleware impls · tiers · signals     ← kernel, fetch, parse
web.parse     bytes → Document                                            ← kernel
web.fetch     Request → Snapshot  · the middleware framework · backends    ← kernel
web.kernel    data models · event bus · bounded loop                      ← (pydantic only)
```

`web.parse` depends on the kernel only (it never sees a Snapshot); the `Snapshot → Document`
bridge lives in `web.resolve`, which has both.

## Data contracts

```python
Request   { url, method, headers, cookies, body, timeout, follow_redirects }         # web.fetch
Snapshot  { request, url, status, headers, content: bytes, elapsed,                   # web.fetch
            set_cookies, redirects, events: [Event], error }   .ok
Document  { content: bytes, kind, url, encoding }   .text                             # web.parse
          .select(css)->Element|None  .select_all(css)->[Element]  .links()  .json()
Element   .text  .html  .attr(name)  .select(css)  .select_all(css)                   # web.parse
Signal    { name, confidence, detail }                                               # web.resolve
```

**`Snapshot` (fetch's output) carries the transport facts.** **`Document` (parse's output) is
pure content** — no status/headers/error; those stay on the Snapshot and are used internally by
resolve. The bridge `web.resolve.document(snap)` just hands parse the bytes.

---

## Layers

### web.kernel — data + the event bus (depends on: pydantic)
- `WebError` / `WebException` / `err(code, msg, **detail)` — structured errors.
- `Event` (base; each layer subclasses) + `EventBus` (sync pub/sub over dotted topics).
- **The ambient bus:** `emit(event)` publishes to the bus active in the current context (a free
  no-op otherwise — works across `await` via a `ContextVar`); `Trace()` is a `with`-scope that
  installs a bus and collects everything emitted (`with Trace() as t: … ; t.events`). Layers call
  `emit(...)` at key points; nothing threads a bus through call signatures.

### web.fetch — `Request → Snapshot`, backends + the middleware framework
Fetch is *just fetch*: it only worries about its **backend**. It knows nothing of tiers,
escalation, or policy.
- `Fetcher` (protocol): `async fetch(Request) -> Snapshot` ; `async aclose()`. Never raises for a
  transport failure — that becomes `snapshot.error`. Fetch does transport **only** (no sniffing /
  decoding; a 404 is a valid Snapshot).
- Backends (each just a Fetcher):
  - `HttpFetcher(*, verify=True, proxy=None, fingerprint=False)` — httpx. `fingerprint` sends
    browser-like headers (real TLS/JA3 impersonation is a heavier backend that plugs in here).
  - `BrowserFetcher(*, headless=True, channel="chromium", proxy=None, fingerprint=False, scripts=(DOM_RECORDER,))`
    — Playwright. `channel="chrome"` is the real Chrome; `fingerprint` a light stealth pass.
    `open(Request) -> LivePage`.
  - `LivePage` — a live page: `click/type/wait_for` return **Self** (drive without snapshotting);
    `snapshot()` materialises a Snapshot, draining page scripts into events.
- Capture (no HAR): `Script { name, js, on: "init"|"load", drain }` runs in the page; its output
  is drained into a `DOMEvent`. rrweb is such a script. Responses become `NetworkEvent`s. Both
  land on `Snapshot.events` — enough to replay a fetch later (replay itself is a future layer).
- **The middleware framework** (the mechanism, no policies): `Middleware = async (Request, next)
  -> Snapshot`, `Handler`, and `stack(base: Fetcher, middleware) -> Fetcher`. A middleware-wrapped
  transport is still a Fetcher.

### web.parse — `bytes → Document` (depends on: kernel)
Purely content: sniff kind + charset, build the tree, expose find/extract utilities.
- `parse(content: bytes, *, content_type=None, url="") -> Document` — `url` is only the link base.
- `Document` / `Element` as above. Lazy lxml/JSON, cached; `Element.select` nests.

### web.resolve — `Request → Document`, middleware implementations, tiers, signals
The opinionated orchestration over fetch's framework. Owns everything policy.
- `Resolver(*, ladder=None, profile=None, rate_limit=None, retry=None, paginate=None, middleware=())`
  `.resolve(Request) -> Document`. It stacks the middleware chain around the base tier and parses
  the resulting Snapshot **once** (`document(snap)`).
- **Named slots, ordering baked in** (a consumer cannot misorder), outermost → innermost:
  `custom → paginate → escalate(ladder) → retry → rate_limit → base tier`. A slot takes config
  (`retry=3`, `rate_limit=0.5`) or a ready middleware.
- **The transport is a policy — a `ladder`** (not a fixed fetcher). `ladder[0]` is the base tier;
  the rest are escalation tiers climbed on a block signal. `ladder(*, proxy=None)` builds the
  canonical order: `http · http+fingerprint · [http+proxy] · browser · browser+fingerprint ·
  [browser+proxy] · chrome · [chrome+proxy]` — cheapest first, each backend lazy.
- **Middleware implementations** (consumer-pluggable; these are the reference ones — write your
  own `async (Request, next) -> Snapshot` and stack it the same way):
  - `retry(max_attempts=3, backoff=0.2)` — same request on a transient failure (transport / 429 /
    5xx).
  - `rate_limit(min_interval)` — per-host politeness (innermost, so it throttles every retry/page).
  - `escalate(tiers, *, blocked=None)` — walk the ladder: on a block (bad status, `spa`, or
    `anti_bot`) re-issue the same request on the next tier. (A tier fetch bypasses retry/rate_limit;
    wrap a tier with those if needed.)
  - `paginate_links / paginate_param(name="page") / paginate_clicks(browser, more)` — resolve the
    whole page sequence into ONE merged Snapshot. Stops compose: `until_empty`, `until_match`,
    `first_n` (budget, stateful), `until_repeat` (loop guard), `any_of(*stops)`.
- **`Profile`** bundles the slots into a named, reusable per-vendor unit; combinable with
  `.with_(...)`. A per-slot kwarg overrides the profile.
- **Signals** — clean standalone functions over a `Document`: `spa`, `login_wall`, `pagination`,
  `anti_bot`. Not lumped behind one interface; a caller calls the ones it needs.

### web.crawl — `Goal → Documents` (a frontier over resolve)
- `Goal { start, scope=same_origin, collect=None, max_pages=50 }` — what to crawl. `scope` decides
  which links to follow; `collect` which resolved documents are results (default: all). Seeds/the
  frontier are internal, derived from the Goal.
- `Crawler(resolver).crawl(goal) -> AsyncIterator[Document]` — breadth-first, deduped, streamed.

### web.dsl — the lazy execution engine (depends on: all below)
- `DSL(resolver, *, browser=None)`; `.ref(url) -> Reference`, `.crawl(seeds) -> Crawl`.
- `Reference` (drive): `click/type/wait_for` return **Self**; `.doc()` is the join into the
  Document surface (drives one live page, snapshots once).
- `Document` (read): `select/select_all/text/links/attr/project` record onto a `Plan`; over a
  `select_all` collection a read maps element-wise, and `project(**selectors)` yields row dicts.
- **Four dispatch modes** from one recording: `.collect()` (sync), `await .acollect()` (async),
  the `Reference`/`Document` itself (lazy), `.to_blob()` + `run_blob(blob, resolver)` (API/remote).
  `Plan { url, actions, reads }` is serialisable, which is what makes remote free.

---

## How it composes

- **Middleware onion.** The Resolver assembles the fixed order above. `rate_limit` innermost →
  throttles every retry and every page; `retry` inside `escalate` → retry the static fetch, then
  decide to climb; `pagination` outermost → drives the loop, and each page descends the whole
  chain.
- **Escalation is a ladder walk.** `ladder[0]` fetches (retried, throttled); if blocked, the next
  tier is tried, and so on. Backends are lazy — a browser/chrome tier launches nothing until a
  request actually climbs to it.
- **The DSL never leaks downward.** `dsl.ref(url).click(...).doc().select_all(".row").project(...)
  .collect()` records a `Plan`; `run()` calls the same plain methods everything else uses.
- **Observability via the ambient bus.** Each layer `emit`s its own event as it works —
  `FetchEvent` (web.fetch), `ResolveEvent` (retry / escalate / page, web.resolve), `CrawlEvent`
  (web.crawl). Wrap any run in `with Trace() as t:` to collect the whole cross-layer stream
  (`t.events`) or `t.subscribe(prefix, handler)` for live handling. Emitting is free unless a
  Trace is active.

## Development

Each package is a real distribution (`web-<layer>`, PEP 420 namespace `web.<layer>`). Install
editable into the venv with uv:

```bash
VIRTUAL_ENV=env uv pip install -e packages/<layer>
```

Gate (per package — the `web.` namespace spans dirs, so run mypy/pyright per package):

```bash
env/bin/python -m pytest packages/<layer>/tests -q
env/bin/mypy --strict packages/<layer>/web
env/bin/pyright packages/<layer>/web
```

Every layer's tests run in isolation against a local `pytest-httpserver` (and a headless browser
for the live-page cases); no network.
