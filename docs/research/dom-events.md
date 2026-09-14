# Live browser interaction & DOM/event capture — a case study

*Status: research. How to make this library's live-page + event-capture feature
best-in-class, optimizing for LLM/agent usefulness and end users. Grounds every
recommendation in the current code (`webclient/core/document/live.py`,
`webclient/models.py`, `webclient/events.py`, `webclient/clients/browser.py`) and
keeps the cores + backings + event-bus architecture. No code here — design only.*

Anchors read from source:

- `LiveBacking` (`core/document/live.py`): the interaction set
  (`click`/`write`/`wait_for`/`select`/`select_all`/`evaluate`/`screenshot`), the
  injected `INIT_JS` MutationObserver + `DRAIN_JS`, `drain()`, and `on_load()`
  which shapes the client's load-time console/network into events.
- Event taxonomy (`models.py`): `Event` base (with `topic`, `seq`, `ts`,
  `session_id`/`document_id`/`plan_id`/`node_id`) → `NetworkEvent` /
  `NavigationEvent` / `DOMEvent` / `DOMUpdateEvent` / `ActionEvent` /
  `ConsoleEvent` / `PlanEvent`; `EventBus`/`EventRegistry` (`events.py`).
- `BrowserClient.open` (`clients/browser.py`): installs `PageScript`s by phase
  (`init` before nav, `load` once after, `drain` after replay), captures
  `console` + `request` into a `PageResult`, replays the recorded action chain.
- Read-side facets (`core/document/summary.py`, `models.py`): `RuntimeBacking`
  (`is_spa`/`framework`/`uses_xhr`/`xhr_endpoints`/`dynamic_elements`) and the
  `Probe` facet (resiliency read-side).

---

## 1. State of the art

How leading tools drive a page and capture DOM / network / interaction signals,
and how LLM-agent frameworks let a model perceive and act.

### Driving a page

- **Playwright** (what this repo already builds on) is the modern baseline.
  - *Auto-waiting + locators.* `page.locator(...)` is lazy and re-queried on use;
    every action (`click`, `fill`) waits for the element to be attached, visible,
    stable, enabled, and to receive events before acting. This is strictly better
    than a bare `wait_for_selector` + act, and it is the model our `_aact` already
    leans on (we call `loc.click(timeout=…)`).
  - *Web-first assertions.* `expect(locator).to_have_text/to_be_visible/…` retry
    until true or time out — a first-class "wait for a **condition**" primitive,
    not just "wait for a selector".
  - *Network routing / interception.* `page.route(url, handler)` +
    `route.fulfill/continue/abort` intercept **every** XHR/fetch/document request,
    to mock, modify, block, or record. HAR record/replay (`routeFromHAR`, a
    `.zip` with payloads) gives deterministic offline replay.
  - *Tracing.* `context.tracing.start(...)` produces a `trace.zip` — a
    **DOM-snapshot-per-action** timeline plus console, network, and source — viewed
    in Trace Viewer. This is the gold standard for "what happened, step by step".
  - *ARIA / accessibility snapshots.* `locator.aria_snapshot()` yields a **YAML
    accessibility tree** (roles + accessible names + values), and since Playwright
    1.52 nodes carry a stable `ref` so an action can target `aria-ref=…`. This is
    the token-efficient, human-meaningful page view agents want.
- **CDP (Chrome DevTools Protocol)** is the lower layer under all of this. It
  exposes typed event streams the high-level APIs wrap: `Network.*`
  (requestWillBeSent/responseReceived/loadingFinished, with bodies),
  `Page.*` (frame/lifecycle), `DOM.*` + `Runtime.*`, and `Accessibility.getFullAXTree`.
  Agent stacks that need request bodies, precise timing, or the full AX tree talk
  CDP directly (`page.context.new_cdp_session(page)` in Playwright).
- **Managed browser platforms — Browserless / Browserbase.** Remote,
  horizontally-scaled Chrome over CDP/WebSocket with session recording, live
  view, proxy/stealth/captcha, and `/function` endpoints. The relevant lesson for
  us: the *browser is a heavy, separately-scaled resource reached over a wire* —
  exactly the `RemoteWebClientCore` core-swap seam the resiliency design already
  names.

### Capturing DOM / network / interaction signals

- **rrweb** is the reference for *session recording/replay*. It emits one **full
  DOM snapshot** (every node assigned a numeric id) followed by compact
  **incremental snapshots** — DOM mutations (via `MutationObserver`, the same API
  our `INIT_JS` uses), plus mouse move/click, scroll, input, and viewport — each
  referencing nodes by that stable id. Replay reapplies increments over the full
  snapshot in a sandboxed iframe. Two ideas transfer directly: (1) a **stable
  numeric node id** assigned once and reused across every later event; (2) a
  **full snapshot + deltas** model instead of our fire-and-forget mutation counts.
- **CDP Network domain** captures the request/response *lifecycle* with bodies and
  timing — richer than Playwright's `page.on("request")` (method/url/type only),
  which is what our `PageResult.network` currently records.

### How LLM-agent frameworks let a model perceive + act

The central finding across current agent stacks (browser-use, WebVoyager,
Project Mariner, and the "serialize the a11y tree" writeups): **agents perceive a
page primarily through a serialized accessibility/DOM tree of *interactive*
elements, each tagged with a stable index/ref, and act by that index** — often
layered with a screenshot for spatial/visual disambiguation.

- **browser-use / a11y-tree agents.** Build a pruned tree of interactive nodes
  (role, accessible name, state), assign each a numeric index `[N]`, render it as
  a compact indented/text list, and expose `click(index)` / `type(index, text)`.
  The framework keeps an index→DOM-node map; the model never sees CSS selectors.
  Serialization strips presentational classes, collapses identical siblings, and
  indexes by interactive role — "structural truth without rendering noise". This
  is far more token-efficient and robust than raw HTML or a screenshot alone.
- **WebVoyorager / set-of-marks.** Screenshot with numbered bounding boxes over
  interactive elements (visual counterpart of the indexed tree) — vision model
  reads the marks and acts by number.
- **The recurring trade-off** (agent-serialization writeups): too raw → the model
  drowns in implementation detail; too pretty → it loses the structure and,
  crucially, the **ref needed to act**. The winning shape pairs a *readable* view
  with a *stable handle* per element.
- **What signals matter to the model, token-efficiently:** the interactive-element
  tree (perceive + act target); *what changed after my action* (did a panel open,
  a row appear, a request fire, an error log?) correlated to the action;
  navigation/URL changes; and terminal conditions (blocked, login wall, empty).
  Console/network noise matters mostly as **error signal** and **XHR endpoints**,
  not as a full firehose.

---

## 2. Our current implementation — honest assessment

### What we have (and it is a clean base)

- **Interaction set** — `click` / `write` / `wait_for` / `select` / `select_all`
  / `evaluate` / `screenshot`, all on the eager sync surface, bridged to the
  engine loop. Auto-waiting comes for free via Playwright locators
  (`loc.click(timeout=…)`). Missing-target policy is sane (`optional=` swallows a
  timeout; loud `LookupError` otherwise). Actions are **recorded** onto the
  `Reference` (`_ref.actions`) and **replayed** on `reload()` — a real,
  deterministic re-materialization, tested in `test_reload_reproduces_state`.
- **Event taxonomy + bus** — typed pydantic events over a prefix-matched topic
  tree, an `EventBus` that stamps `seq` (per `document_id`) + `ts` and filters by
  correlation ids, and an `EventRegistry` that resolves topics (incl. plugin
  namespaces like `rrweb.dom.update`) to classes for typed round-tripping. Events
  are queryable off the document: `events`, `events_of(type|topic)`,
  `action_events`, `console`, `dom_mutations`.
- **Capture** — `INIT_JS` installs one `MutationObserver` per navigation into
  `window.__wc_mutations`; `drain()` settles 30ms, reads+clears the buffer, and
  turns each record into a `DOMUpdateEvent` (kind added/removed/attribute/text +
  the ancestor `id` path). Console + network are captured by the client
  (`page.on("console"/"request")`) into `PageResult` and shaped by `on_load` into
  `ConsoleEvent` / `NetworkEvent` (xhr/fetch only).
- **Per-element narrowing** — `select("#c1")` filters the parent's
  `DOMUpdateEvent`s to those whose captured `ids` include the node's id, so a
  `LiveNode` sees only its own mutations (`test_livenode_event_narrowing`). The
  `Event.node_id` field exists for stable narrowing but is **not yet stamped**.
- **Read-side derivation** — `RuntimeBacking` already projects captured events
  into an agent-friendly `Runtime` facet (`is_spa`, `framework`, `uses_xhr`,
  `xhr_endpoints`, `dynamic_elements`), and the `Probe` facet is wired for the
  resiliency layer.
- **Clean layering** — `PageScript` phases (`init`/`load`/`drain`) make script
  injection a transport concern while the *what* stays in the backing's
  `page_scripts`; the client leases a page and never shapes events itself.

### Gaps (measured against §1)

1. **No accessibility / interactive-element snapshot — the biggest agent gap.**
   There is no way for a model to *perceive* the page as a compact indexed tree.
   Vision is screenshot-only (`_ashot`), and acting requires the caller to already
   know a CSS/xpath selector. Modern agents want `snapshot() → indexed a11y tree`
   + `act by index`. Playwright's `aria_snapshot()` + `aria-ref` is right there and
   unused.
2. **DOM capture is lossy and identity-free.** `DOMUpdateEvent` records only a
   *kind* + an **ancestor-`id` path** + added/removed **counts** — not which node,
   what tag/role/text, or old→new attribute values. Nodes without an `id` are
   invisible to narrowing. There is no stable node identity across mutations
   (`node_id` is declared but never set), so an agent can't ask "what is the
   element that just appeared?" or diff a subtree. This is well below rrweb's
   node-id + full-snapshot model.
3. **No network request interception / mocking / routing.** We only *observe*
   `page.on("request")` (method/url/resource_type). No `page.route`, no
   fulfill/abort/modify, no HAR record/replay. Response status, bodies, and timing
   aren't captured (`NetworkEvent.status_code`/`body` exist on the model but are
   never populated for browser sub-requests). Deterministic offline replay and
   "block the tracker / mock the API" are impossible.
4. **Limited wait strategies.** `wait_for` handles selector-appears or a fixed
   timeout only. No wait-for-**condition** (text/visible/hidden/count), no
   network-idle, no custom-JS predicate, no wait-for-navigation/URL, no
   wait-for-load-state. Agents routinely need "wait until the spinner is gone" /
   "until the row count changes".
5. **No assert / expect primitives.** There is no retrying assertion
   (`expect_text` / `expect_visible`). Agents and users hand-roll
   `wait_for` + `select` + compare, which is racy.
6. **No session recording / replay trace.** We replay the *action chain* to
   re-fetch, but we never record a viewable timeline (DOM snapshot per step +
   correlated console/network) — no Playwright-trace / rrweb equivalent for
   debugging or for feeding an agent "here's what happened".
7. **Weak action↔effect correlation.** `_aact` publishes an `ActionEvent`, then
   `drain()` appends `DOMUpdateEvent`s — but nothing links them: the mutations
   carry no back-reference to the action that caused them, and `ActionEvent`
   carries no summary of its effect (mutations produced, requests fired, console
   errors). This is exactly the "what did my click do?" signal agents need most.
8. **Console/network capture is load-time only + narrow.** `on_load` shapes the
   console/network snapshot the client took *during navigation*. Console messages
   and requests produced by *later interactions* aren't continuously streamed onto
   the document (the page listeners live in `open`, not for the page's life);
   there's no NavigationEvent emitted on in-page navigation, no response side, no
   failed-request capture.
9. **Screenshot-only vision, no set-of-marks.** `screenshot()` returns a raw PNG;
   there's no annotated/numbered-overlay variant to pair with an indexed tree.
10. **`evaluate` is an untyped escape hatch.** Powerful but opaque to the agent;
    everything not covered by an op falls to raw JS strings.

---

## 3. Best-in-class target (prioritized, LLM-agent-first)

What a *fully implemented* live/events feature looks like here. Ordered by
agent-usefulness-per-unit-effort.

### Tier A — perceive + act like a modern agent (highest leverage)

- **A1. An accessibility / interactive-element snapshot view.** A
  `snapshot(mode="a11y" | "interactive" | "dom")` op that returns a compact,
  token-efficient tree of interactive nodes — role, accessible name, state, and a
  **stable ref/index** per node — plus a companion `act_by_ref(ref, action, …)`.
  Back it with Playwright `aria_snapshot()` + `aria-ref` (or CDP
  `Accessibility.getFullAXTree` for full control). This is the single biggest
  upgrade: it lets a model *see* the page as browser-use/WebVoyager agents do and
  act without pre-knowing selectors. Render it as an on-document facet so it
  composes with `summary()`.
- **A2. Stable node identity across mutations.** Assign every observed node a
  stable id once (rrweb-style), stamp it onto `DOMUpdateEvent.node_id`
  (and the snapshot refs), and key per-element narrowing on `node_id` instead of
  the ancestor-`id` heuristic — so narrowing works for id-less nodes and survives
  re-renders. This is the connective tissue for A1, richer DOM events, and
  correlation.
- **A3. Action↔effect correlation.** Give every action a correlation id (reuse
  `plan_id`/a new `action_id`), stamp it onto the `DOMUpdateEvent`s /
  `NetworkEvent`s / `ConsoleEvent`s produced in that action's `drain` window, and
  attach an **effect summary** to the `ActionEvent` (`{mutations: n, added: [...],
  requests: [...], console_errors: [...], url_changed: bool}`). Then an agent's
  loop is: act → read one compact "what changed" record. This is the perception
  signal agents value most, and we already have the drain seam to build it.

### Tier B — richer, safer capture

- **B1. Richer DOM events.** Extend `DOMUpdateEvent` to carry the target's
  `node_id`, tag/role, a short text/label, the changed attribute name + old→new
  value, and a CSS-ish path — not just counts. Optionally a full-snapshot +
  incremental model (rrweb event types) behind a capture level, so a subtree can
  be reconstructed/diffed.
- **B2. Network routing + full request/response capture.** A `route(pattern,
  action)` op (`fulfill` / `abort` / `modify` / `record`) via `page.route`, and a
  full-lifecycle `NetworkEvent` (status, headers, timing, optional body) via
  `page.on("response")`/CDP — populating the `status_code`/`body` fields the model
  already has. Enables mocking, tracker-blocking, and HAR record/replay for
  deterministic tests.
- **B3. Continuous, lifetime capture.** Keep console/network/navigation listeners
  attached for the page's whole life (not just `open`), streaming onto the
  document via the bus — so post-interaction logs, failed requests, and in-page
  navigations (`NavigationEvent`) are captured, not just the load snapshot.

### Tier C — richer control + observability

- **C1. Wait/assert primitives.** `wait_for(state="networkidle"|"load")`,
  `wait_for(selector, state="visible"|"hidden"|"detached")`,
  `wait_for(predicate="<js>")`, `wait_for_url(...)`, and retrying assertions
  `expect_text` / `expect_visible` / `expect_count` (Playwright web-first
  assertions). Removes the racy hand-rolled loops.
- **C2. Session trace.** An opt-in recorder that snapshots the (a11y or DOM) view
  per action and bundles it with the correlated console/network into a portable
  trace (our own light format, or wrap Playwright `tracing`), for debugging and
  for handing an agent a replayable history.
- **C3. Set-of-marks screenshot.** A `screenshot(marks=True)` variant that
  overlays numbered boxes on the interactive nodes from A1, so a vision model can
  act by the same indices — the visual twin of the indexed tree.

---

## 4. Concrete recommendations for THIS codebase

Keep the invariants: **behaviour in backings, remote is a core-swap, one shared
surface, events over the bus, scripts declared by the backing.** Everything below
names the exact seam.

### R1 (Tier A1) — `snapshot()` op + a `snapshot` render mode. **Do first.**

- **Seam:** a new `LiveBacking` op `snapshot(core, mode="interactive")` in
  `provides` + `io` (it awaits the page). Body: `await core._page.locator("body")
  .aria_snapshot()` (or a CDP `Accessibility.getFullAXTree` via
  `page.context.new_cdp_session`), transformed into a typed value model.
- **Value model:** `AriaNode` / `PageSnapshot` in `core/document/models.py`
  (alongside `Element`/`Runtime`), each node `{ref, role, name, state, children}`.
  Add it to `FACETS` so `summary(include="snapshot")` composes, and expose a
  matching render mode (`render("a11y")`) for the token-efficient string an agent
  reads.
- **Act-by-ref:** extend `_aact` to accept `ref=` and resolve it via Playwright's
  `aria-ref=<ref>` locator, so `click(ref=…)` / `write(ref=…, text)` work without a
  CSS selector. Record `{"op": "click", "args": {"ref": …}}` on `_ref.actions` for
  replay parity.
- **Static parity (nice-to-have):** an offline a11y-ish tree from the parsed HTML
  in `HtmlBacking` so `snapshot()` also works on a static document (degraded: no
  live state), keeping the "same surface across cores" property.

### R2 (Tier A2) — stable `node_id`, stamped in `INIT_JS` + `drain`.

- **Seam:** extend `INIT_JS` to tag each observed target with a stable
  `data-wc-id` (assign on first sight, monotonic counter on `window`), and include
  it in the pushed mutation record. `drain()` (`live.py`) reads it and sets
  `DOMUpdateEvent.node_id`. Change per-element narrowing in `_aselect` to filter
  on `node_id` (falling back to the current `ids` path) so id-less nodes narrow
  correctly.
- This unblocks R1's refs, R3's richer events, and R4's correlation with one
  low-risk change to two JS strings + `drain`.

### R3 (Tier A3 + B1) — action↔effect correlation + richer `DOMUpdateEvent`.

- **Seam:** in `_aact`, generate an `action_id`, publish the `ActionEvent`, run the
  interaction, then in `drain(core, action_id=…)` stamp that id (reuse the
  existing `plan_id`/`node_id` correlation fields, or add `action_id` to `Event`)
  onto every event drained in that window. After draining, compute an effect
  summary and set it on the `ActionEvent` (extend `ActionEvent` with an
  `effect: dict` / typed `Effect`).
- **Richer records:** widen `INIT_JS` to also capture, per mutation, the target's
  tag/role, a short text/label, and (for `attributes`) the attribute name +
  `oldValue`/new value (`MutationObserver` `attributeOldValue: true`,
  `characterDataOldValue: true`); surface them in `DOMUpdateEvent.detail`. Purely
  additive to the event model.
- **Payoff:** `action_events` (already on `EventBacking`) become a compact
  agent-readable "what my click did" log, and `events_of(DOMUpdateEvent)` narrowed
  by `action_id` answers "what changed because of *this* step".

### R4 (Tier B2/B3) — network routing + lifetime capture.

- **Seam (routing):** a `route(core, pattern, action, *, body=None)` op on
  `LiveBacking` wrapping `page.route` → `fulfill`/`abort`/`continue`. Emit a
  `NetworkEvent` (new topic e.g. `network.route`) for each interception so the
  bus/registry see it.
- **Seam (capture):** move the `page.on("console"/"request")` wiring out of the
  one-shot `BrowserClient.open` into a lifetime install (still transport-owned;
  the backing declares intent), add `page.on("response")` to populate
  `NetworkEvent.status_code`/headers/body, and emit `NavigationEvent` on in-page
  navigation (`page.on("framenavigated")`). Stream all via `core._client.bus` onto
  `core._events`, matching how `on_load` shapes today's snapshot.
- **Registry:** these are additive topics — `EventRegistry.resolve` already
  degrades unknown `network.*` to `NetworkEvent`, so old consumers keep working.

### R5 (Tier C1) — wait/assert primitives.

- **Seam:** extend `wait_for` in `LiveBacking` with `state=` (map to
  `page.wait_for_load_state` / `locator.wait_for(state=…)`) and a `predicate=` JS
  option (`page.wait_for_function`); add `wait_for_url`. Add `expect_text` /
  `expect_visible` / `expect_count` ops backed by Playwright's retrying assertions
  (catch the assertion timeout → the same `LookupError`/optional policy `_aact`
  uses). All go in `provides` + `io`.

### R6 (Tier C2/C3) — session trace + set-of-marks. **Last.**

- **Trace seam:** an opt-in recorder that, per `_aact`, captures the R1 snapshot +
  the R3 effect summary into a `PlanEvent`-like `TraceEvent` stream on the bus; a
  `trace()` facet assembles them. Optionally wrap Playwright `context.tracing` for
  a `trace.zip` when a real browser trace is wanted. New `PageScript` `drain`-phase
  work is unnecessary — this rides the existing action loop.
- **Set-of-marks seam:** a `screenshot(core, marks=True)` branch in `_ashot` that,
  before shooting, injects boxes/labels over the interactive nodes from R1 (a
  `load`-phase or ad-hoc `evaluate`), so the PNG's numbers match the snapshot refs.

### Sequencing & risk

R1+R2 together are the step-change for agents and are low-risk (one new op, two JS
edits, one facet). R3 builds on R2 and delivers the "what changed" signal. R4 is
the most transport surgery (lifetime listeners + `page.route`) and should be its
own pass with a test matrix (mirror the `resiliency.md` per-request-transport
caution). R5 is small and independently shippable. R6 is polish. Every step is
additive to the event taxonomy (new topics degrade gracefully via
`EventRegistry.resolve`) and touches no static-document behaviour, preserving the
core-swap and shared-surface invariants. Remote parity: because these are backing
ops + typed events, `RemoteWebClientCore` gets them for free once the events
serialize across the wire (same argument as `ProbeRecord` in `resiliency.md`).

### Tests to add (mirroring `tests/test_browser.py` / `tests/test_events.py`)

- `snapshot()` returns an indexed interactive tree; `click(ref=…)` acts by ref
  and records a replayable action.
- A mutation to an id-less node still narrows to its `LiveNode` via `node_id`.
- After a click, the `ActionEvent` carries an effect summary and the drained
  `DOMUpdateEvent`s share its `action_id`.
- `route()` fulfills a mock and aborts a blocked request; a `network.response`
  event carries a status code.
- `wait_for(state="hidden")` and `expect_text(...)` retry then pass/raise per the
  optional policy.

---

## 5. Sources

- Playwright — ARIA / accessibility snapshots (`aria_snapshot`, `aria-ref`,
  `toMatchAriaSnapshot`): <https://playwright.dev/python/docs/aria-snapshots>,
  <https://playwright.dev/docs/aria-snapshots>; ref attribute discussion:
  <https://github.com/microsoft/playwright/issues/35650>
- Playwright — network interception / mocking (`page.route`,
  `fulfill`/`continue`/`abort`, HAR): <https://playwright.dev/docs/mock>,
  <https://playwright.dev/docs/network>
- Playwright — tracing / Trace Viewer (`trace.zip`, DOM snapshot per action):
  <https://playwright.dev/docs/trace-viewer-intro>
- Playwright — auto-waiting + web-first assertions (`expect(locator)`):
  <https://playwright.dev/docs/actionability>, <https://playwright.dev/docs/test-assertions>
- CDP — Accessibility (`getFullAXTree`) and Network domains:
  <https://chromedevtools.github.io/devtools-protocol/tot/Accessibility/>,
  <https://chromedevtools.github.io/devtools-protocol/tot/Network/>
- rrweb — full snapshot + incremental mutation recording/replay, numeric node ids:
  <https://github.com/rrweb-io/rrweb>,
  <https://github.com/rrweb-io/rrweb/blob/master/docs/observer.md>,
  <https://rrweb.com/glossary/incremental-snapshot>, <https://rrweb.com/glossary/full-snapshot>
- How agents perceive pages — serializing the accessibility tree, indexed
  interactive elements: <https://agentlabs.cc/blog/serializing-accessibility-trees-for-browser-agents>,
  <https://isagentready.com/en/blog/how-ai-agents-see-your-website-the-accessibility-tree-explained>,
  <https://web.dev/articles/ai-agent-site-ux>
- Browser-agent architectures (DOM + a11y + screenshot layering; Project Mariner):
  <https://arxiv.org/html/2511.19477v1>,
  <https://dev.to/alexey_sokolov_10deecd763/runtime-snapshots-16-the-three-architectures-of-browser-agents-4gkc>
- browser-use (indexed interactive elements, act-by-index): <https://github.com/browser-use/browser-use>
- Managed browser platforms — Browserless / Browserbase (remote CDP, stealth,
  recording): <https://www.browserless.io/>, <https://www.browserbase.com/>
- Related in-repo design: `docs/design/resiliency.md` (the remote core-swap /
  sidecar-browser seam these recommendations reuse).
