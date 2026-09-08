# Open Design Questions

Decisions the implementation needs that `PLAN.md` / `models.py` don't settle.
Each is either **provisionally decided** (I picked something to keep moving —
change it and I'll refactor) or **blocking** (I need an answer before the
milestone that depends on it).

Status legend: 🟢 provisionally decided · 🔴 blocking · ⚪ noted, no action yet

---

## M1 — pure core

### 1. Packaging — 🟢
Created `pyproject.toml` (setuptools backend), distribution name `webclient`,
version `0.1.0`, `requires-python >=3.10` (the interface uses
`typing_extensions.Self`, implying <3.11 support is intended). No packaging
choice was in the plan. Alternatives: hatchling/PDM, `src/` layout.

### 2. Charset-detection dependency — 🟢
M1 requires "encoding detection" but `PLAN.md §4` deps list has no charset
library. Chose **`charset-normalizer`** (httpx's own optional dependency, pure
Python, actively maintained). Alternatives: `chardet`, `cchardet` (faster, C).
Added to `pyproject.toml` dependencies.

### 3. Fate of the root `models.py` / `models.py.orig` — 🟢 DECIDED
`models.py.orig` deleted (git history at `a21da85` is its archive). Root
`models.py` stays as the frozen spec (option a) until the package reaches
interface parity (~M6), then gets deleted; no `import *` shim — two
importable sources of the same names is how drift starts.

### 4. `Reference.replace()` validation — 🟢
Implemented with `model_copy(update=...)`, which does **not** re-validate —
`ref.replace(port="nan")` would not raise until use. Matches pydantic's
`model_copy` contract and is fast. Alternative: full re-construct + validate.

### 5. `Reference.url` reconstruction — 🟢 (three sub-points)
  - **Query re-encoding:** `from_url` stores *decoded* param values; `.url`
    re-encodes via `urllib.parse.urlencode`. Round-tripping a URL with
    reserved characters in the query can change the exact byte string
    (semantically equivalent). 
  - **Empty path:** `Reference(hostname="e.com").url` → `https://e.com` (no
    trailing `/`). Some origins expect `/`.
  - **Fragment:** emitted verbatim, not percent-encoded.
  Confirm these are acceptable.

### 6. Typed-view object model — 🟢
`doc.html` / `.json` / `.xml` / `.binary` return a **cached** sibling object
built with `model_construct` (no re-validation) that **shares the parent's
private state** (`__pydantic_private__`) — so binding, the parsed-tree cache
and the event list are shared, and mutating one view's private state affects
the original. DECIDED: aliasing is the intended semantics (views of one
document), but the shared-private-state mechanism is undocumented pydantic
behavior — **pin it with a regression test** (`doc.html.events is
doc.events` etc.) so a pydantic upgrade can't silently break it. Longer
term, consider views holding a parent reference instead of being siblings.

### 7. XML parsing is lenient — 🟢
`XMLDocument` parses with `lxml.etree.XMLParser(recover=True)` — malformed XML
is silently repaired rather than raising. Consistent with how browsers treat
HTML; inconsistent with strict XML expectations. Toggle?

### 8. `Node.attr()` on a missing link attribute — 🟢 DECIDED (spec updated)
Unified error philosophy: **loud by default, leniency opt-in via
`optional=True`**, everywhere. `attr(name)` raises `LookupError` when
missing; `attr(name, optional=True)` returns None. Extended to fetches:
`fetch()`/`reload()` raise `FetchError` on transport failure or non-2xx
unless `optional=True`, which returns the (not-ok) Document for inspection
via `.ok`. Spec now defines `FetchError` and the `optional` params.

### 9. `Node.events_of` element-narrowing semantics — 🟢 DECIDED (spec updated)
**Event scoping via capture-stamped node identity.** `Event` gained
`node_id` (stable node identity, e.g. rrweb node ids, stamped by capture
plugins). Static `Node.events_of` = document scope, unfiltered (narrowing
is only well-defined for live capture); `LiveNode.events_of` narrows: an
event matches when its `node_id` resolves to this node or a descendant
(ancestor-path prefix over the capture plugin's node ids). M4's dom/rrweb
plugin must stamp node ids on dom/action events; narrowing gets its own
test. PLAN §2.2/§2.3 updated.

### 10. `XMLDocument.xpath` non-element results — 🟢 DECIDED (spec updated)
Simple, single interface: **no separate `xpath()` method** (removed from
spec). `select()`/`select_all()` accept CSS or XPath (auto-detected:
leading `/` or `./` = XPath) on any document; selection returns **elements
only** — XPath producing attributes/text/scalars (`.../@href`, `text()`)
raises `ValueError` telling the caller to use `.attr()`/`.text`. Nothing
is silently dropped.

### 11. CSS `.select()` on non-HTML documents — 🟢 DECIDED
Kind-appropriate parser (current impl) is correct; the spec docstring said
"delegates to the html view" and has been fixed. One change required:
`select` on a kind with no tree (json/binary) must raise a **typed** error
("select() requires a parsed tree; this document is json"), not a raw
parse error.

### 12. Stub strategy for not-yet-built methods — 🟢
Every method a later milestone owns (`fetch`, `reload`, `paginate`, `render`,
all `LiveDocument`/`LiveNode` actions, `EventBus`, `EventRegistry`,
`ClientPool`, `Expr`, `Executor`, `WebClient` lifecycle) raises
`NotImplementedError("<feature> lands in M<n>")` today, rather than returning
`None`/`...`. Keeps failures loud and self-documenting.

### 13. `events.py` ↔ `models.py` coupling — 🟢
Split per `PLAN §1`. `events.py` has **no runtime import** of `models.py`;
`NetworkEvent.request: Reference` is resolved by a `model_rebuild` call at the
bottom of `models.py`. Consequence: importing `webclient.events` *without*
`webclient.models` leaves `NetworkEvent` unresolved (can't instantiate).
Normal `import webclient` loads `models` first, so this only bites unusual
import orders. Acceptable?

### 14. `HTMLDocument.links()` attribute fallback — 🟢
Default selector `a[href]` per interface, but the implementation also reads
`src`/`action` when `href` is absent (helps when a custom selector is passed).
Harmless; note in case you want strict href-only.

---

## Discovered early, needed later

### 15. `Surface.emit` correlation-tagging contract — 🟢 DECIDED
`emit` **overwrites** correlation ids with the surface's own (single source
of truth — a buggy plugin must not be able to mis-route events into another
document) and sets `source` to the plugin name. The **bus** stamps both
`seq` and `ts` (one clock, one counter). NOTE: supersedes #23 below, which
was implemented as fill-blanks before this decision landed — M2 code needs
the flip.

### 16. Renderer registry key when `formats` overlap — 🟢 DECIDED
Same `name` replaces the plugin wholesale. A different-named plugin claiming
an occupied `(kind, format)` shadows it: **last-registered wins, with a
warning log**. Deterministic, matches "replacement by registration".

### 17. `default_client()` lifecycle — 🟢 DECIDED (amended)
Lazy init under a lock; recreates on next call after `.close()` — as
implemented. Amendment to the no-atexit choice: add a **best-effort
`atexit` close** (guarded, ignore errors). Rationale: the daemon loop dies
with the process but playwright browser subprocesses are not guaranteed to
— graceful teardown when we get the chance. References bound to a closed
default client fail loudly rather than resurrecting it.

---

## M2 — engine loop + http + plugins

### 18. Event-loop scope: per-WebClient — 🟢 DECIDED
**One loop per WebClient**, approved. Isolation on close beats one thread
saved. PLAN §0 wording updated.

### 19. httpx client model — 🟢
`PLAN §0` says "httpx.AsyncClient per http lease". Implemented as a **bounded
reusable set** (up to `max_http` persistent `AsyncClient`s; `acquire` borrows,
`release` returns). A borrowed client is exclusive to its lease, so the
network plugin can safely mutate its `event_hooks` for the request's
duration. Preserves connection pooling; a strict "new client per request"
would not.

### 20. Retry policy — 🟢 DEFERRED
Transport-error-only retry stands for now. A real policy (5xx, 429 +
`Retry-After`, exponential backoff + jitter) is a deliberate post-v1
feature — more complex than it looks; do not grow it incrementally.

### 21. What the http-path network plugin emits — 🟢
`NavigationEvent` for the final response; `NetworkEvent` (base) for each
redirect hop. No `AssetEvent` / `XHREvent` on the http path (httpx fetches no
sub-resources — those are browser-only, M4). The plugin reads the response
body in an httpx response-hook (`await response.aread()`), relying on httpx
caching the body for the caller's subsequent read.

### 22. Event routing onto documents — 🟢
On fetch the client subscribes a **catch-all** handler
(`bus.subscribe("", …, document_id=doc_id)`) that appends every correlated
event to `doc.events`. The `Document.events` list and the engine's internal
per-id list are the **same object**. Per-topic cap hardcoded at
**1000** (`PLAN §5` wants "a config knob" — deferred to M4).

### 23. `Surface.emit` tagging contract — 🔴 SUPERSEDED by #15 decision
Implemented as fill-blanks / never-overwrite before the #15 decision
landed. The decided contract is the reverse: **emit overwrites correlation
ids** with the surface's own and always sets `source` to the plugin name;
bus stamps `seq` + `ts` unconditionally. M2 code must flip to match
(exception: `node_id` is stamped by the plugin, not the surface — the
plugin is the only party that knows the element).

### 24. `EventRegistry.resolve` ancestor search — 🟢 (resolves earlier)
Order: exact topic → drop trailing segments → drop a leading segment and
repeat. Gives `rrweb.dom.update → dom.update` (the models.py example) and
`network.xhr.slow → network.xhr`. The leading-drop is somewhat loose (a
made-up `foo.console` would resolve to `console`); flag if you want it
stricter (e.g. only drop a leading segment that matches a known plugin
namespace).

### 25. `WebClient` infra fields use `default_factory` — 🟢
`pool` / `bus` / `registry` / `plugins` are `Field(default_factory=…)` rather
than the interface's literal `= ClientPool()` — mandatory, because they hold
`threading.Lock`/counter state pydantic would try (and fail) to deep-copy per
instance. Field **types** are unchanged.

### 26. `text` renderer is not a readability port — 🟢 DEFERRED
The heuristic is acceptable for now — the renderer is a plugin, so a
readability-backed replacement is registration, not surgery. Revisit when
output quality matters.

### 27. `elements` / json-`elements` renderers are hand-rolled — 🟢
`elements` walks block-level DOM nodes into typed `Element`s (title/text/
list_item/code/table/image) — not an Unstructured integration. The
`json → elements` renderer (my design; plan only names it) flattens the
parsed JSON into `path → value` leaf blocks with `parent_id` links.

### 28. `run_coro` re-entrancy guard always on — 🟢
Calling a sync facade method from the loop thread (i.e. from a bus handler)
raises `RuntimeError` immediately (`PLAN §5` bridge-deadlock). `PLAN §2.4`
says "debug mode" — I made it unconditional (the check is one comparison).

---

## M4 — browser

### 29. `Plugin.attach_async` additive hook — 🟢
Some plugins need *awaited* setup on a page surface (playwright
`page.expose_binding` for the DOM plugin's `__wc_emit`). The spec's
`Plugin.attach(surface)` is sync. Rather than make the whole plugin surface
async, the engine calls an OPTIONAL `attach_async(surface)` coroutine right
after the sync `attach`, on the loop. Plugins that don't need it don't
define it; the sync `attach` stays the documented contract. Flag if you'd
rather `attach` itself became async.

### 30. Live event capture races with `goto` — 🟢 (design note)
Console/XHR/DOM events fire *during* navigation, before a LiveDocument
object exists. Fix: routing is subscribed to a temp list *before* `goto`
(mirrors the http path), then the list is adopted into `live.events` and
routing re-pointed — safe because publish and this swap both run on the one
loop thread (no interleave).

### 31. DOM capture is a hand-rolled MutationObserver, not rrweb — 🔴
`PLAN §2.2/M4` names rrweb as the swap-in DOM plugin and the acceptance
gate ("zero engine changes"). I shipped the *interface* proof instead: a
`PageDomPlugin` using an injected MutationObserver that stamps `node_id`
identity paths and emits periodic `dom.snapshot` checkpoints with a digest,
plus a test (`test_custom_page_plugin_needs_no_engine_changes`) proving an
external plugin needs no engine changes. A real rrweb plugin
(`plugins/rrweb.py`, bundling rrweb's JS, richer snapshots) is NOT yet
written. Is the hand-rolled capture enough for now, or do you want the
actual rrweb integration before M5?

### 32. `screenshot`/`evaluate`/`execute` beyond strict M4 scope — 🟢
Implemented `evaluate` (JS with return), `execute` (chainable side-effect),
element + full-page `screenshot`, `back`/`forward` history, and the full
interaction set (check/select_option/upload/drag/scroll/press) now, since
they're thin playwright wrappers and demo/tests exercise them. No new
decisions; flagging scope.

---

## M7 — service

### 33. Raw-CDP passthrough (WS /sessions/{id}/cdp) deferred — 🔴
The spec/plan list a raw CDP websocket onto a session's BrowserContext
(Browserbase/string.ai/Spider compatibility). NOT implemented this pass:
it needs the chromium CDP endpoint proxied through the websocket and the
context's `newCDPSession`, which is a meaningful chunk on its own. Every
other M7 endpoint (sessions, fetch, render, select, plans, events WS) is
done. Want the CDP passthrough as a follow-up?

### 34. Service must hold documents (weakref mismatch) — 🟢
The library registers static Documents by *weakref* (ISSUES decision: the
caller keeps them). The service returns only an id and keeps nothing, so
documents were GC'd between requests. Fix: the service keeps its own
bounded LRU strong-ref cache (cap 256) of documents it fetched. This is a
service-layer concern; the library's weakref policy is unchanged.

### 35. Plan submission runs synchronously — 🟢
`POST /plans` runs the plan and returns all rows, rather than the spec's
detached submit + `GET /plans/{id}` polling + row streaming. Simpler and
fine for modest plans; detached execution with a plan registry and the
`/plans/{id}/rows` + `/plans/{id}/stream` endpoints is a follow-up when a
plan's runtime warrants async submission.

---

## RemoteWebClient

### 36. lxml / charset-normalizer imports made lazy — 🟢
Moved into the parsing sites (`_parsed`, `Node.html`, `Document.text`,
`_select_elements`) so importing `webclient.models` -- and therefore the
lazy layer and the remote client -- no longer loads the native lxml wheel.
playwright was already lazy. Verified: with lxml+playwright import-blocked,
`RemoteWebClient` and the full `q` plan API still import and build.

### 37. Remote parity is structural, not nominal — 🟢
`RemoteWebClient` / `RemoteDocument` / `RemoteSession` are NOT subclasses of
the eager types; they duck-type the same method names. Rationale: a remote
`select` can't return an in-process `Node`. Contract: the **plan API**
(`q` + `collect`/`execute`) is 100% portable (identical rows local or
remote, proven by a cross-check test); the **imperative document surface**
is best-effort and chatty -- render, one-level select (`select`/
`select_all` with optional `attr`), `.text`/`.title` work; deep nested
selection should lower to a plan. Deps: httpx + pydantic only.

### 38. Remote tests use a real uvicorn thread — 🟢
`httpx.ASGITransport` is async-only, so a sync `httpx.Client` can't drive
the ASGI app in-process. Tests boot uvicorn on an ephemeral port in a
daemon thread -- which also exercises the true HTTP path -- and point the
RemoteWebClient at it.
