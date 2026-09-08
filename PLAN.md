# Implementation Plan

The interface in `models.py` is the frozen v0 surface. This plan turns it into
a working engine. Guiding rule: **KISS** — one async engine, one event bus,
one sync facade, one plugin pathway for all capture and rendering, an id for
every long-lived object. No abstraction that isn't forced by the interface.

## 0. Architecture

```
user code (sync) ──┐
REPL / scripts     ├──> WebClient (facade, registries, lifecycle root)
HTTP/WS service ───┘        │
     ┌──────────┬───────────┼───────────┬──────────────┐
  Sessions   ClientPool   EventBus   Plugins        Executor
     │          │            ▲       (attach to        │
     │          ▼            │        surfaces,        │ schedules
     │    engine (one asyncio loop,   emit events,     │
     │          daemon thread)        render docs)     │
     │    ├── engine/http.py     httpx.AsyncClient per http lease
     │    └── engine/browser.py  1 playwright browser,
     │                           1 BrowserContext per session,
     └───────────────────────────1 Page per page lease
```

### Threading model (the KISS bridge)

- One asyncio event loop **per WebClient**, in a daemon thread, started
  lazily. All I/O is async inside the engine. (Isolation: closing one
  client can never starve another; ISSUES #18.)
- Every public facade method is implemented as `async def _method` on the
  engine side plus a thin sync wrapper:
  `asyncio.run_coroutine_threadsafe(coro, loop).result(timeout)`.
- Streaming (paginate, `collect(stream=True)`, `run(stream=True)`) bridges
  with a `queue.Queue` fed from the async side; the sync iterator blocks on
  `queue.get()`; a sentinel closes it. Backpressure = bounded queue.
- The service (M7) is async-native: FastAPI handlers await the same
  `_method` coroutines directly, skipping the bridge. This is why the split
  into `async _method` + sync wrapper is mandatory from day one.
- Bus handlers run on the publisher's thread (the loop thread for engine
  events). Handlers must not block and must not call sync facade methods
  (documented; enforced with a loop-reentrancy assert in debug mode).

## 1. Package layout

Single-file `models.py` splits at M1 into:

```
webclient/
  __init__.py       # public API: WebClient, q, col, lit, model re-exports
  models.py         # data: Reference, Document(+views), Node, LiveNode, Element
  events.py         # Event taxonomy, EventBus, EventRegistry, Subscription
  plugins/
    base.py         # Surface, Plugin, Renderer
    core.py         # core capture plugins: network, console, action, dom
    render.py       # core renderers: html->markdown/text/elements/links, ...
    rrweb.py        # RRWeb dom-capture plugin (replaces core dom plugin)
  pool.py           # ClientPool, Lease
  session.py        # Session
  client.py         # WebClient facade, registries, default_client()
  engine/
    loop.py         # loop thread, sync<->async bridge, stream bridge
    http.py         # request(lease, ref) -> Document
    browser.py      # page management, surface creation, actions, replay
  lazy/
    expr.py         # Expr recorder, Lazy, q, QueryPlan (de)serialization
    executor.py     # compile() + scheduler
  service/
    api.py          # M7: FastAPI app mapping REST/WS onto WebClient
tests/
```

## 2. Wiring

### 2.1 fetch, http path

```
ref.fetch()
 └─ resolve client: ref.bound or default_client(); session: ref binding or wc default
 └─ wc.fetch(ref, session=...)  →  bridge  →  engine._fetch:
     1. lease = await pool.acquire("http", session)     # bounded, FIFO queue
     2. request = merge(ref, session.headers/cookies/proxy, wc.default_headers)
     3. resp = await lease.httpx_client.request(...)    # retries per wc.retries
     4. the core network plugin (attached to the "transport" surface)
        emits NetworkEvent/NavigationEvent via surface.emit
     5. doc = Document(id=uuid4().hex, session_id=s.id, kind=sniff(content_type),
                       content=..., final_url=..., **ref fields)
     6. "document" surface created → document-kind plugins attach
     7. wc._documents[doc.id] = weakref(doc);  pool.release(lease)
```
HTTP leases are held only for the duration of the request. `Document.kind`
sniffing: content-type header first, leading bytes as fallback.

### 2.2 fetch, browser path

```
1. lease = await pool.acquire("page", session)
   - playwright browser started lazily (one per WebClient)
   - one BrowserContext per session, created on first page lease for that
     session, storage_state loaded if the Session carries one
   - Page created/reused up to pool.max_pages (global bound)
2. "page" surface created BEFORE goto: engine installs each attached
   plugin's `scripts`, then calls plugin.attach(surface). Core page plugins:
   - network: page.on(request/response) + CDP session → XHR/Fetch/
     Navigation/AssetEvent
   - console: page.on(console) → ConsoleEvent
   - dom (default): MutationObserver init script reporting via a binding →
     DOMLoad/Update/UnloadEvent. Replaced by the rrweb plugin when
     registered: rrweb source as init script, emits DOMSnapshotEvent
     (full snapshots + digest, periodic checkpoints) and DOMUpdateEvent
     subclasses (incrementals)
   - action: interactions record + emit ActionEvent (emitted facade-side)
   Emit contract (ISSUES #15): the surface OVERWRITES correlation ids with
   its own and sets `source` to the plugin name; the bus stamps `seq` and
   `ts` unconditionally.
   Event scoping: dom/action capture plugins additionally stamp
   `Event.node_id` — a stable node identity (rrweb node ids) — which is
   what LiveNode.events_of narrows on (ancestor-path prefix: an event
   matches a node when its node_id is the node or a descendant). Static
   Node.events_of stays document-scoped; narrowing exists only where live
   capture assigned identities.
3. wc registers routing subscriptions for the new document id:
   bus.subscribe(topic, doc_append_handler, document_id=id)
   -> the ONLY mechanism filling Document.events (typed views read it)
4. await page.goto(ref.url, wait_until=...)
5. LiveDocument(id, session_id, lease_id) registered with a STRONG ref
   (a leased page must never depend on gc)
```

- Every interaction (`click`, `write`, …): record + emit `ActionEvent`,
  then perform the playwright call with auto-wait; return self.
- `navigate()`: same page object, new document id — detach page plugins for
  the old surface / attach for the new, swap routing subs, return a new
  LiveDocument carrying the same lease.
- `wc.release(live)`: detach plugins, cancel routing subs, release lease
  (page cleared and parked, context kept). `Session.close()`: persist
  storage_state, close context, release its leases. `wc.close()`: all
  sessions, browser, loop tasks. A gc finalizer on LiveDocument is a
  warn+release backstop only.

### 2.3 Surfaces & plugins

- Surface instances are created by the engine at these points:
  `client` (WebClient start), `session` (session()), `transport` (httpx
  client creation), `page` (page lease, pre-goto), `document` (Document
  construction), `node` (selection, lazily), `plan` (executor run start).
- `wc.use(plugin)`: appends to `wc.plugins`, registers `plugin.events`
  with the EventRegistry, and — for Renderers — registers
  `(kind, format) -> renderer` in a render table. Attach order ==
  registration order; core plugins are pre-registered and replaced by
  registering a plugin with the same `name`. A different-named plugin
  claiming an occupied `(kind, format)` shadows it — last-registered wins,
  with a warning log (ISSUES #16).
- **All capture is plugins** (§2.2); the engine only creates surfaces and
  calls attach/detach. One pathway to maintain, and swapping naive DOM
  capture for RRWeb is pure registration.
- **Representations are Renderer plugins per document kind**:
  `HTMLDocument.markdown/.elements`, `Document.render("markdown")` and the
  service's `GET /documents/{id}/render?format=...` all resolve through
  the render table. Core renderers (pure functions over the parsed doc):
  html → markdown, text (readability), elements (Unstructured-style typed
  blocks), links, html; json → elements; xml → text, elements.
- Plugin events integrate with the lazy layer via the EventRegistry:
  `events_of` ops in plans name event types by topic; compile resolves
  them through the registry (server-side too), so plans referencing plugin
  events (de)serialize and validate.

### 2.4 EventBus

- In-process, synchronous dispatch: `dict[topic-prefix, list[_Sub]]` under
  a lock; publish walks subs whose topic is a dotted prefix of the event's
  and whose correlation filters match.
- The bus stamps `seq` (per-document counter) at publish; events without a
  document id get a per-client stream counter. Consumers that need
  buffering (the WS API) bring their own `asyncio.Queue` in their handler.
- Deliberately the simplest thing satisfying "everything shares one bus";
  revisit only if a real bottleneck shows.

### 2.5 Executor — compile

`QueryPlan.steps` is a tree (map/then fields hold sub-plans). Compilation:

- Walk the tree; every op becomes an `ExecutionStep`.
- Dependencies: chain order gives a linear dep; `col("x")` adds a dep on the
  step producing field x (so `then()` fields form a DAG, evaluated in
  declaration order only where col-deps demand it).
- Resource tagging: op `fetch` → `"http"`; `fetch(browser=True)` → `"page"`;
  live actions (`click`/`wait_for`/…) → `"page"` with the same `page_group`
  as their upstream live fetch; pure ops (select/attr/render/…) → `None`.
- `map()` compiles its body once as a template subgraph; fan-out is runtime.
- Record-time validation (in the recorder, before compile ever runs): each
  recorded op name/signature is checked against the eager class of the
  current context type via `inspect.signature`; `events_of`/`render`
  arguments resolve through EventRegistry / the render table. Typos fail
  at authoring time, not at collect time.

### 2.6 Executor — run (execution order)

```
ready = steps with no unmet deps
while unfinished:
    for step in ready:
        if step.resource: lease = await pool.acquire(step.resource, session)
        schedule task(step, lease)
    on task completion: mark done, publish plan event, extend ready
```

- Pure steps run inline on the loop (they're microseconds of parsing).
- `page_group` steps share one lease and run strictly in plan order.
- `map` fan-out: when the upstream list step completes, instantiate the
  template subgraph per element. In-flight rows bounded by
  `max_inflight_rows` (default `2 * pool.max_http`) so memory stays flat.
- Rows complete → per-step `OnError` applied (skip drops the row, ignore
  yields None for the field, raise cancels the plan) → pushed to the result
  stream. Unordered by default; `ordered=True` buffers to input order.
- `Executor.status()` served from counters updated on task completion;
  `cancel()` cancels outstanding tasks and releases their leases.

Worked example (the `_example_lazy` plan): the root fetch takes 1 http
lease; per-card title/is_active/link are pure steps fanned out per element;
rows failing `filter` stop there; each surviving row's `content` step
acquires a page lease (max 4 cards in the browser stage at once), its
click/wait_for/select serialize on that page, `date` is pure and depends on
`content`; rows stream out as each card's subgraph finishes.

### 2.7 Service (M7) — browser as a service

Thin FastAPI adapter over the facade; no logic beyond (de)serialization and
auth. Designed against the field: session lifecycle ≈ Browserbase/Steel,
representations ≈ Firecrawl/Spider/Unstructured output formats, raw CDP
escape hatch ≈ string.ai `/wss` / Spider `/v1/browser` / Browserbase
`connectUrl`. Where they return page content inline, we return **handles +
on-demand representations** — full HTML crosses the network only when
explicitly rendered as `html`.

| Endpoint                        | Facade call / behavior                |
|---------------------------------|---------------------------------------|
| POST /sessions                  | `wc.session(**body)` — accepts `ttl`, `keep_alive`, proxy, headers; returns id + status + `expires_at` |
| GET  /sessions/{id}             | status/lifecycle (`pending→running→expired/closed`) |
| DELETE /sessions/{id}           | `session.close()`                     |
| WS   /sessions/{id}/cdp         | raw CDP passthrough onto the session's BrowserContext. Capture plugins hook at context level, so CDP-driven activity still emits events |
| POST /fetch                     | `wc.fetch(Reference(**body))` → document id + metadata (status, kind, final_url). Optional `formats=[...]` inlines representations in the response |
| GET  /documents/{id}            | `wc.document(id)` metadata            |
| GET  /documents/{id}/render     | `doc.render(format, **options)` — markdown / text / elements / links / html, plus any plugin-registered format |
| POST /documents/{id}/select     | server-side `select_all` + attr/text extraction → values only |
| POST /documents/{id}/actions    | `live.replay([ActionEvent...])` — remote live interaction *is* the replay format |
| POST /plans                     | `wc.execute(QueryPlan(**body))`, detached; returns plan id |
| GET  /plans/{id}                | `executor.status(id)`                 |
| GET  /plans/{id}/rows           | collected/paged rows (jsonl)          |
| WS   /plans/{id}/stream         | row stream as produced                |
| WS   /events?topics=&session=&document=&after= | `bus.subscribe(...)` → socket stream |

**Event streams clients can rebuild from** (the Steel lesson: incremental
replay diverges when an event is missed):
- every event carries `seq` (per-document, monotonic); `after=seq` resumes
  a stream and lets clients detect gaps;
- the dom/rrweb plugin emits periodic `dom.snapshot` checkpoints (full
  serialized DOM + `digest`), so a consumer resyncs from the latest
  snapshot instead of replaying history;
- `digest` lets a client verify its rebuilt DOM matches the server's.
- events over the wire are typed by topic; receivers resolve classes via
  the EventRegistry, unknown topics degrade to their nearest ancestor.

Auth: bearer token middleware; one WebClient per token (or shared, config).
Diagnostics headers on every response (`x-request-id`, duration, bytes) à
la string.ai. A stateless one-shot `POST /fetch` convenience (auto-session,
inline actions+formats, Firecrawl/string.ai-shaped) is **post-v1** — pure
composition of session+fetch+render+release, no new machinery. A remote
python client implementing the `WebClient` surface over these endpoints
comes after that; the interface already permits it because everything is
addressed by id.

## 3. Milestones

Each milestone ships importable + tested before the next starts.

- **M1 — pure core, no I/O.** Package split. `Reference.url/join/
  with_params/replace`, `Document` views + selection (`lxml` +
  `cssselect`: one parser dep covers CSS for HTML and XPath for
  XMLDocument), `Node`, encoding detection. Golden-file HTML fixtures.
- **M2 — engine loop + http + plugin framework.** `engine/loop.py` bridge,
  `pool.py` (http side), `events.py` (bus + registry + seq stamping),
  `plugins/base.py`, transport/document surfaces, core network plugin,
  core renderers (markdown/text/elements/links — pure functions, easy
  tests), `client.py` with fetch/reload/use, `default_client()`. Tests
  against `pytest-httpserver` (no network).
- **M3 — sessions + static pagination.** Session lifecycle
  (ttl/keep_alive/status, cookie persistence httpx jar ↔ Session),
  `paginate` with `prefetch` concurrency and `resume`, event routing onto
  documents, pool stats.
- **M4 — browser.** `engine/browser.py`: page pool, contexts per session,
  page surfaces + core page plugins (network/console/dom/action),
  LiveDocument actions/wait/navigate/screenshot, storage_state
  round-trip, `replay`. Capture plugins stamp `node_id` on dom/action
  events; `LiveNode.events_of` narrowing gets its own test (click a child,
  assert the parent's node sees it and a sibling's doesn't). Then
  `plugins/rrweb.py` as the first external-style plugin — it must require
  zero engine changes (that's the test of the plugin interface). Tests
  serve local static pages with JS.
- **M5 — lazy recorder.** `Expr` op recording with record-time signature
  validation (registry-aware for `events_of`/`render`), `QueryPlan`
  serialization round-trip, `q` namespace, `__dir__` completion.
- **M6 — executor.** compile (deps + resources + page groups), scheduler,
  streaming, `OnError` policies, `status`/`cancel`, RunStats. Tests: plans
  against the M2/M4 local servers, plus pure-compile unit tests asserting
  execution order on synthetic graphs.
- **M7 — service.** `service/api.py` as §2.7: sessions lifecycle, CDP
  passthrough, render/select endpoints, resumable event WS with
  snapshots, plan submission. Remote smoke test: fetch → render markdown
  → run a plan → rebuild a page from the event stream and verify digest.

## 4. Decisions taken (revisit only with cause)

- Async-first engine, sync facade. The interface promises pools and
  prefetch concurrency; that's async either way, so build it there once.
- All capture and all representations go through the plugin pathway; the
  engine only creates surfaces. Core capture/renderers are plugins,
  replaceable by name.
- Core event taxonomy is closed and inheritable (NetworkEvent → XHR/Fetch/
  Navigation/Asset; DOMEvent → Load/Snapshot/Update/Unload); plugin events
  subclass it; topics are dotted strings matched by prefix.
- Documents over the wire are handles; content moves as representations.
- `lxml` (+`cssselect`) for parsing — CSS and XPath from one dependency.
- Ids are `uuid4().hex`. Registries: weakrefs for static Documents, strong
  refs for LiveDocuments until released.
- Wire format is `QueryPlan` JSON, versioned (`version: 1`) from the start.
- Lazy chains validate at record time, because `Expr.__getattr__` typing
  can't catch typos.
- Retry policy is deliberately minimal: transport errors only, no backoff.
  A full policy (5xx / 429 / Retry-After / jitter) is post-v1 (ISSUES #20).
- Error philosophy, uniform (ISSUES #8): **loud by default, leniency via
  `optional=True`** — missing element/attr raises; fetch raises FetchError
  on transport failure or non-2xx; `optional=True` returns None (selection
  / attr) or the not-ok Document (fetch, inspect `.ok`).
- One selection interface (ISSUES #10): `select`/`select_all` take CSS or
  XPath (leading `/` or `./` = XPath), elements only — attribute/text/
  scalar XPaths raise ValueError (use `.attr()` / `.text`). No separate
  `xpath()` method. Selecting on a treeless kind (json/binary) raises a
  typed error, not a parse error.
- Event scoping (ISSUES #9): `Event.node_id` stamped by capture plugins;
  LiveNode narrows events by node-identity ancestor prefix; static Node is
  document-scoped.
- Deps: runtime `pydantic`, `httpx`, `playwright`, `lxml`, `cssselect`,
  `markdownify` (or hand-rolled in the html renderer); service adds
  `fastapi`, `uvicorn`; dev `mypy`, `pytest`, `pytest-httpserver`.

## 5. Risks / watch items

- **Bridge deadlock**: user code calling sync facade methods from a bus
  handler running on the loop thread. Documented + debug assert (§2.4).
- **Page leaks**: leases are explicit; finalizer warnings + `pool.stats()`
  make leaks visible early.
- **map fan-out memory**: bounded by `max_inflight_rows` (§2.6).
- **Event-store growth**: `Document.events` capped per topic with a config
  knob (snapshot events retain only the latest N) before M4 ships.
- **rrweb divergence**: mitigated by seq gaps + periodic `dom.snapshot`
  checkpoints + digest verification (§2.7); replay correctness gets its
  own test (rebuild page from stream, compare digest).
- **CDP passthrough observability**: raw CDP users bypass the facade, not
  the capture — plugins hook at BrowserContext level. Verify with a test
  driving the passthrough and asserting events still arrive.
