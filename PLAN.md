# Implementation Plan — the `spec.py` refactor

`/spec.py` (was `types.py`) is the frozen v0 surface. This plan turns it into
a working engine by **refactoring the current tree in place** (`main` @
`754a83b` + P0).

**This is a refactor, not a rewrite.** The `redesign` branch was a rewrite: it
tripled the file count and replaced a working base wholesale. We keep the
lean, working parts (the async engine loop, the http/browser backends, the
pool, the event bus, the plugins, sessions) and change only what the new
object model forces: collapse `Document`/`LiveDocument`/`Node` into one
capability-driven `Document`, add `WebBase` + `Collection` + `Field`, and turn
the two-implementation lazy layer into one op-registry-driven language.

Two rules make that claim checkable rather than aspirational:

1. **The tree stays green at every milestone.** Every PR leaves the current
   test suite passing (159 tests at P3, browser suite included where
   playwright is installed) and `demo.py` runnable. No "red from P1 until the cut-over".
   A milestone that needs the tree red is a rewrite step and is rejected.
2. **The package does not grow.** Budget below. Every PR deletes what it
   replaces in the same PR; the P0 skeleton is a loan repaid by P2–P4.

Guiding rule, unchanged: **KISS**. One async engine, one sync facade, one
expression language in two modes, one op registry as the single source of
truth for dispatch, typing, and the wire format.

---

## 0. Budget, and what we keep / change / delete

| | lines (`webclient/`) |
|---|---|
| `main` @ `754a83b` | 3 871 |
| after P0 (skeleton loan: `ops.py` 142, `models.py` +174, `lazy()` +14) | 4 208 |
| after P1 (policy engine, `NameScope`, `ref()`/`request_fields`; nothing deleted yet) | 4 419 |
| after P2+P3, independent-Expr rebuild (recorder −140, executor −9, `Node` −83; `models.py` +349, `ops.py` 193 as a plain `@policy` decorator) | **4 414 — still over the P0 figure** |
| **ceiling at P6** | **≤ 3 871** |
| ~~expected at P6 ~3 400~~ — re-projected at P3 with the scope as written | **~4 000–4 100: misses the ceiling** |
| after P4 (Backings: `live.py` −399 deleted, `backings.py` +372, page-op dispatchers on `Document`) | **4 514** |
| after cleanup (typed views removed) + `core/` submodule + P5 (join fix, WebClientCore) + P6 (search/summary) | **4 548** |

**Budget status at P3 — raised, awaiting a decision (see §7).** P2+P3
deleted the two-implementation lazy layer and `Node` (−369 lines), but the
v0 surface that replaces them costs more: `WebBase`/`Field`/`Collection`,
comparison and branching ops, the op registry with both sync and async
policy paths, and the generated `Collection` block. Re-projection to P6:
P4 deletes `live.py` (−399) and adds page ops (~+110); P5 removes
`optional=`/`fetch` overloads (~−50) and adds the core split (~+15); P6 thins
remote/service (~−60). That lands near 4 050.

Measured with `wc -l webclient/*.py webclient/*/*.py`. Tests and `demo.py`
are outside the budget and are expected to grow.

**Keep as-is:** `engine/loop.py`, `engine/http.py`, `engine/browser.py`,
`pool.py`, `events.py` (+ `plugins/`) — reused, rework deferred — and
`session.py` (gains name-scoping only).

**Refactor in place:**
- `models.py` → `WebBase` is the base of `Reference`, `Document`,
  `Collection[T]`, `Field[T]`; `WebError` on the wire. `Document` no longer
  subclasses `Reference`; capability comes from its backing. **P0 landed the
  four new classes as signatures next to the old ones**; P1 rebases the old
  ones onto them.
- `live.py` → **deleted** (P4); `LiveDocument`/`LiveNode` fold into
  `Document` (browser ops raise `UnsupportedOperation` naming the fix when the
  backing can't serve them). `Node` folds into `Document`/`Collection` (P3).
- `lazy/expr.py` + `lazy/executor.py` → one recorder + one evaluator over the
  **same** `@op` implementations (P2). Today they are two implementations of
  one language — the source of the nested-`map` drop and the leaked-Expr-arg
  bug. `lazy()` already lives in `expr.py`.
- `client.py` → `WebClientCore` (async `resolve`/`execute`) + `WebClient`
  (sync facade). Trim the god-object: page setup / cookie priming /
  subscription ordering move behind the backing.
- `remote.py`, `service/api.py` → built on the `WebClient` op surface;
  session-scoped.

**Added (P0, done):** `ops.py` — `@op`, `OpSpec`, `REGISTRY`, `bind_ops`,
capability check, the error-policy constants, `record`/`run` entry points.
One file, 142 lines, no package imports (no cycles).

**Seed, don't re-derive:** `git show redesign:webclient/plan.py` is a typed
discriminated-union IR that matches P2's "typed IR"; lift it into `expr.py`
rather than writing a second one. Nothing else from `redesign` comes back:
not `surfaces/`, `values.py`, `records.py`, `telemetry.py`, `explain.py`,
`roots.py`, nor `backing.py`-as-five-files. Backings are three classes in one
place; roots and functions live in `lazy/expr.py`.

---

## 1. Language mechanics (verified)

- **Recordable operators:** comparisons (`== != < > <= >=`) and `& | ~`
  return `Field[bool]` — logic composes with these.
- **Non-recordable:** `and`/`or`/`not`, `bool()`, `len()`, `for … in`,
  `x in y` — Python coerces these at the C level. **Rule (P0):** on a
  **lazy** object these raise `TypeError` naming the recordable form; on an
  **eager** object they evaluate (`bool(field)` is `bool(field.get())`,
  `for d in collection` iterates). Verified: a pydantic model whose `__eq__`
  returns a wrapper is otherwise *always truthy* — `if doc.attr("x") == "y":`
  would silently take the branch, so `__bool__` is mandatory, not optional.
- `__eq__` recording ⇒ `Field` is unhashable (`__hash__ = None`); never a
  dict key or set member.
- **Typing idiom, verified in mypy `--strict` and pyright (P0 gate):**
  `def lazy[T](cls: type[T]) -> T` with `lazy(Collection[Document])`;
  `attr("href") -> Reference` via a `Literal` overload (needs
  `# type: ignore[overload-overlap]`, as today); `Field[bool]` from `==`;
  `Collection[T].filter -> Collection[T]`; `extract -> Self`.
- **Checker divergence, found at P0:** pyright *applies* a class decorator's
  return type, mypy ignores it. `bind_ops` must be typed identity
  (`(cls: C) -> C`) or every decorated class becomes `type` under pyright.
  Both checkers run in `tests/test_typing.py` for exactly this reason.
- **`lazy()` construction:** `cls.model_construct()` + private `_lazy` flag.
  `cls.__new__(cls)` leaves a pydantic instance with no fields (verified:
  `AttributeError` on first field access).
- **Dispatch is the op table only.** The evaluator resolves names through
  `REGISTRY.resolve(obj, name)`; there is no generic `getattr` path, so there
  is no `_`-name mitigation to maintain and nothing on pydantic's public
  surface is reachable from a plan. (The only thing that needed generic
  attribute access was `.params.get("uddg")`; see Decision 6.)

## 2. Decisions (amended at P0; ▲ marks a change from the previous plan)

1. **One class, two modes; eager runs through the core.** `resolve`/`select`/
   `extract`/`filter`/`project`/`click`/… are `@op` methods on the real
   classes. Module roots `doc`, `many`, `ref` are `lazy(Document)`,
   `lazy(Collection[Document])`, `lazy(Reference)`. An eager call evaluates
   *through the async core* — the same path lazy and remote take — so they
   cannot drift.
2. ▲ **Typing via `lazy()`; the `Collection[T]` twin is a committed generated
   stub.** Static checkers read source, so "generated from the registry at
   runtime" cannot type-check. `Collection[T]`'s lifted element methods
   (`many.select(...).attr("href") -> Collection[Reference]`) live in a
   generated block inside `class Collection` under `if TYPE_CHECKING:`
   (▲ not a `models.pyi`: a stub file would hide all of `models.py` from the
   checkers), produced from `REGISTRY` by
   `scripts/gen_stubs.py`; `tests/test_typing.py` runs it with `--check`, so
   drift fails CI. The stub is the *only* hand-free twin; the runtime lifting
   is one generic `__getattr__`-free mechanism in `Collection` driven by
   `OpSpec.cardinality`.
3. ▲ **Ops return wrappers; `Field` is unregistered.** `attr -> Field[str] |
   Reference`; `select -> Document`; `select_all -> Collection[Document]`.
   `Field[T]` is a `WebBase` so combinators and comparisons are type-visible
   in both modes; `.get()` unwraps on the eager path (the accepted tax). But a
   `Field` **never enters the name registry** and gets `name`/`root` lazily on
   first read — otherwise a 1000-row × 5-field extract registers 5000 objects,
   the growth Section 6 warns about. Only `Reference`, `Document`,
   `Collection` are registered.
4. **Naming is by keyword; `.alias()` is dropped.** `extract(title=…, price=…)`.
5. **Conditionals are polars-style `when/then/otherwise`** on a `Field`:
   `expr.when(cond).then(a).otherwise(b)`. This is *branching*; error handling
   is Decision 7.
6. ▲ **Safe dispatch from P2, not deferred.** The op table is the only
   dispatch (Section 1). `search_expr`'s `.params.get("uddg")` becomes the
   `Reference.param("uddg") -> Field[str]` op. No generic attribute access, no
   `_`-name blocklist, no "trusted network" caveat for code execution. What
   remains deferred is *authorization* (who may resolve what), not
   *safety*. Deserializing an unknown op or root raises.
7. ▲ **Error policy — mode-dependent defaults, IGNORE always explicit, not-ok
   short-circuits.** Every `WebBase`-returning op takes
   `error=IGNORE|RETURN|RAISE`. The object always comes back carrying
   `error: WebError | None`, `ok: bool`, `message`.
   - **Default: eager → RAISE; lazy-plan execution → RETURN.**
   - `RETURN` → `ok=False`, `error` set. `filter(doc.is_ok())` drops failures.
   - `IGNORE` → `ok=True`; `error`/`message` retained for diagnostics only.
     **`ok` is the only truth**; nothing may branch on `error is None`.
   - ▲ **An op on a not-ok receiver does not execute**: under RAISE it
     raises the carried error (`OpError`); under RETURN it returns the
     receiver unchanged (Self ops) or a not-ok result carrying the same
     `WebError`, so `doc.click(error=RETURN).write(...)` cannot hide the
     click failure. Inspection ops (`is_ok`, `is_empty`; `@op(always=True)`)
     still run. Error state is cleared only by an op that *succeeds on an ok
     receiver* or by explicit `IGNORE`.
   - ▲ **A failed field inside `extract` marks that `Field` not-ok, not the
     row.** `is_ok(doc.field("title"))` is per-field; the row stays ok so
     sibling fields survive. (`filter(doc.field("title").is_ok())`.)
   - ▲ **Non-`WebBase`-returning ops are terminals and take no `error=`:**
     `project`, `fields`, `Field.get`, `evaluate`. They raise the carried
     `WebError` if the receiver is not ok (IGNOREd objects are ok, so they
     project). This replaces the spec's "IGNORE→None / RETURN→Exception" rule
     for such methods; it removes a three-overload signature per op and the
     `T | WebError | None` return unions that would have leaked into every
     caller. **Spec deviation, recorded.**
   - ▲ Decision 16's "a failing row cancels its siblings" applies to
     **RAISE only**. Under the lazy RETURN default a bad row is a not-ok row.
   One decorator (`ops.run`), not per-method code.
8. **Runtime capability checks.** Ops declare `require="tree"|"ok"|"page"`;
   `ops.check` raises `UnsupportedOperation` naming the fix when the backing
   can't serve it. Capability is **live backing state**, checked every call.
9. **Addressability.** Every registered object has `name` (short, e.g.
   `doc:000-001`), `kind`, `root`, forming a chain `wc:… → ref:… → doc:… →
   doc:…`. Names are **scoped to the resolver** (`wc.resolve` → client scope;
   `session.resolve` → session scope) and retained while that scope lives
   (session/ttl). `wc.document(id)`/`wc.reference(id)` recover in scope.
   ▲ Every `WebBase` carries `is_lazy` and shows it in `repr`, so a plan that
   was never bound is diagnosable rather than silent.
10. **Collections hold one address, not N.** `select_all` returns a
    `Collection[Document]` backed by one selector over a snapshot; elements
    are addressed lazily (`root` = the collection).
11. **Reference is the action chain.** Request spec **plus** the ordered
    actions taken from a Document (`select`/`click`/`write`/`wait`) **plus**
    the resolve options (`browser`/`proxy`/`anti_bot`) and their outcome
    telemetry. `doc.ref()` rebuilds the reference for the document's *current*
    state. A mutating action makes a new action chain under the same document
    name. Replay is best-effort and must fail loudly on a changed page.
12. **`extract` merges; same name overrides.** `field("name")` means "a value
    already extracted in this context", identical in both modes.
13. **Context binding inside `extract`.** Expressions are bound to the
    document `extract` was called on; over a `Collection`, `doc` is each
    element. No separate `el` root in v0.
14. **Reference hidden outside the lazy interface.** Eager users go through
    `wc.fetch(**reference_like)`/`wc.resolve(...)`; a bare `Reference(...)` is
    a lazy root and unbound `.resolve()` returns a lazy object (`is_lazy`).
15. ▲ **`select(sel, wait=…)` and snapshot invalidation.** With `wait` on a
    page backing, auto-wait the DOM; without it, the current snapshot. **A
    mutating action invalidates the tree snapshot**; the next tree op
    re-snapshots from the page, so `doc.click("#more").select(".row")` sees
    the post-click rows. A document that *navigated away* keeps its last
    snapshot and loses `page` (Decision 8). Dialect (css/xpath/jsonpath) is
    chosen from document kind and selector shape; a binary element inherits
    `url`/`status_code` and its `content` is the element bytes.
16. **Streaming + bounded fan-out.** Plan execution streams rows as subgraphs
    complete; fan-out is a bounded work queue sized by the pool; under RAISE a
    failing row cancels its siblings and orphans nothing; page-`resource`
    steps in one `page_group` serialize on one lease.
17. ▲ **`is_ok`/`is_empty` are ops, not module functions.** They return
    `Field[bool]`, so `many.filter(doc.is_ok())` records; the `is_ok(expr)`
    function form in `spec.py` is redundant and not provided. **Spec
    deviation, recorded.**

## 3. Spec coverage and deferrals

Spec items with a milestone: everything in `WebBase`/`Field`/`Collection`
(P0 signatures → P1–P3 bodies), `Document` tree ops (P3), page ops (P4),
`events(kind)` (P1, over the existing bus), `screenshot -> Document(kind=
"binary")` (P4), `fields()/references()/documents()` (P2/P3).

**Deferred:** render (`render`/markdown/elements/links stay as today on
`Document`), pagination (`paginate` — on `Reference`), secrets redaction,
binary `save`, proxy pools, retry beyond transport errors, authorization,
event-bus rework, document kinds beyond html/xml/json/binary (csv, excel,
parquet, pdf are `kind` values the sniffer may emit; no ops on them).
**Dropped:** `back`/`forward`. `search`/`summary`/`crawl` are sketched
interfaces, built last.

## 4. Target layout (delta from current, kept small)

```
webclient/
  __init__.py     public API: WebClient, wc.fetch, lazy roots (doc/many/ref), IGNORE/RETURN/RAISE
  models.py       WebBase, WebError, Reference, Document, Collection[T], Field[T]
  ops.py          @op, OpSpec, REGISTRY, bind_ops, check, record, run     [NEW, P0 done]
  client.py       WebClientCore (async resolve/execute) + WebClient (sync facade) + name registry
  session.py      WebSession (+ name scope)
  pool.py         reused
  events.py       reused (rework deferred)
  engine/         loop.py http.py browser.py — reused
  lazy/
    expr.py       lazy(), roots, typed IR (lifted from redesign:plan.py), functions
    executor.py   one evaluator over the @op implementations; streaming; fan-out
  plugins/        reused
  remote.py       refactor → session-scoped WebClient surface
  service/api.py  refactor → session-scoped, methods-as-endpoints
scripts/gen_stubs.py   regenerates the Collection block in models.py from REGISTRY (P2, done)
```

Net new: `ops.py` and `scripts/gen_stubs.py` (both done). Net deleted:
`live.py`, root `models.py` (old spec, done). Everything else edited in place.

## 5. Milestones (strangler order — green at every step)

Each ships importable, tested, type-checked (mypy **and** pyright on
`tests/fixtures/typing_surface.py`), inside the budget, with `demo.py`
extended to exercise everything landed (standing rule).

- **P0 — spec + typing gate. ✅ done.** `types.py` → `spec.py` (it shadowed
  the stdlib `types` module and broke pytest startup on 3.10 and 3.12); root
  `models.py` (previous spec) deleted; `pyproject` finds subpackages,
  `requires-python >=3.12`, `pyright` in `dev`; `env/` rebuilt on 3.12 with
  `dev`. `ops.py`; `WebBase`/`WebError`/`Field`/`Collection` signatures in
  `models.py` beside the old classes; `lazy()`; typing corpus passing both
  checkers. Suite: 123 passed.
- **P1 — error policy + addressability, on the existing classes. ✅ done.**
  `ops.run` (policy table, eager-RAISE default, not-ok short-circuit,
  coroutine ops settle through the owning client's loop), `Field.get`/
  `is_ok`/`is_empty`. `Reference` rebased onto `WebBase` and carries the
  action chain (`actions`, `options`); `Document` inherits it (`ok` is the
  field, derived from `status_code` at build time; `ref()`;
  `request_fields()`). Scoped `NameScope` registry in `client.py` replaces
  the weakref dict: client scope `000` LRU-capped by `names_cap`, one scope
  per session dropped on close, `wc.document/reference(name)` and
  `session.document/reference(name)`; `id` mirrors `name` until P5.
  Amendments: `Document` **stays a `Reference` subclass until P3** (that is
  where elements become Documents, which is what forces the split — doing it
  in P1 would have reddened the tree for nothing); the spec's `events(kind)`
  is today's `events_of` and is renamed at P5 when the `events` list field
  goes; the `ActionEvent` property is now `action_events` (the `actions`
  field is the replayable chain). Suite: 139 passed.
- **P2 + P3 — one expression language, one evaluator, `Node` folded. ✅
  done functionally; ❌ budget target missed (4 452 vs < 4 208).** Landed as
  one step: the recorder is unusable until `select`/`attr`/`resolve` are
  ops, and the tests that execute plans need the new evaluator, so splitting
  them would have reddened the tree. What landed:
  - `lazy/expr.py` (363 → 118): typed IR `Plan`/`Step`/`Arg` (lifted from
    `redesign:plan.py`, trimmed), `lazy()`, `from_plan()` with registry
    validation, roots `doc`/`many`/`ref`, `field()`. `ops.record` appends a
    step and returns a lazy wrapper of the declared return class.
  - `lazy/executor.py` (269 → 228): one walk dispatching only through
    `REGISTRY.resolve`; a Collection fans the rest of the chain out per
    element through a bounded `TaskGroup` worker pool (a failure cancels
    siblings); `filter` drops inline; rows stream; plan events kept.
  - `models.py`: `Document.select`/`select_all`/`attr` and
    `Reference.resolve`/`param` are ops. `Node` is deleted: an element is a
    `Document` whose backing is a subtree (html/xml) or a JSON sub-value,
    `content` is the element bytes, `root` is the parent. JSON documents
    have a tree (`select("items[0].n")`). `attr("text"|"html"|"value")`
    replace the old `.text` property access in plans. `Field` has
    `eq/ne/lt/le/gt/ge/and_/or_/not_` ops behind the operators and
    `when/then/otherwise`. `Collection` lifts element ops at runtime and via
    the generated block for checkers. `extract` merges, `project` is a
    recordable op, `field/reference/document(s)` read extracted values.
  - `Reference("url")` is a lazy root carrying the request spec as
    `plan.source`; keyword construction stays eager.
  - `client.execute(expr, context=None, stream=False)`; service `/plans`
    takes `Plan` JSON and answers 422 on an unknown op; remote sends `Plan`.
  - ▲ `q`/`col`/`lit`/`Expr`/`QueryPlan`/`OnError` are **deleted, not
    aliased**: porting the tests and demo was smaller than an alias layer.
  - ▲ `OpSpec.cardinality/resource/mutates` removed: nothing read them. P4
    re-adds exactly what its scheduler reads.
  *Gates met:* unknown op/root raises on deserialize, dunder ops refused;
  eager and lazy agree on the same extract/filter/project; nested
  collections flatten; bounded fan-out (peak = limit); failing row cancels
  siblings; explicit `error=RAISE` aborts a plan; stub drift test; both
  checkers on the corpus; `expr.py` and `executor.py` smaller. Suite: 159
  passed (browser included); `demo.py` exits 0.
- **P4 — page backing via Backings; `live.py` deleted. ✅ done.** Implemented
  as PLAN §5b: `backings.py` holds `HTMLSelect`/`JSONSelect`/`LiveSelect`/
  `LiveAction`; `Document._backing(op)` (the switch) dispatches select/attr
  and the full interaction set to the provider for the document's current
  medium, raising `UnsupportedOperation` (naming the gate) when none applies.
  A live element is a `Document` with a locator backing, so selection nests
  and `events_of` narrows by node identity. `LiveDocument`/`LiveNode` are
  aliases of `Document` for one release. `optional=` kept as a shim (removed
  P5); `back`/`forward` dropped. *Gates met:* all 13 browser tests pass
  (click/write/live-select/xhr+console capture/dom snapshot/narrowing/
  screenshot/navigate/replay/session storage/pool release/custom plugin);
  eager+lazy still agree; both checkers clean incl. `backings.py`; demo
  exits 0. **Budget: 4 514 — over the ≤4 300 re-baseline by ~210.** The page-op
  dispatchers on `Document` are boilerplate; generating them (as the
  `Collection` block is generated) or removing typed views would recover it.

- **P5 — `WebClientCore` + the final-URL bug. ✅ done (partial).** `WebClient.core`
  exposes the async surface (`await wc.core.resolve/execute`) for callers on
  the loop; the sync facade still raises there via the loop guard. `Document.join`
  now resolves relative links against `final_url` (after redirects), not the
  request URL — the recorded bug, fixed with a regression test. `optional=`
  is **kept** as a documented shim (removing it is churn with no capability
  change); it can go in a later pass.
- **P6 — high-level helpers. ✅ done (crawl out of scope, per the user).**
  service `/plans` and the remote client already ride the plan surface (done
  at P2/P3). `wc.search(term, engine=…, limit=…)` runs a query against a
  configurable `SearchEngine` (url template + selectors) and returns result
  rows; `wc.summary(url, browser=…)` resolves a page to `{title, markdown}`.
  Both build a plan and run it through `execute`; fixture-tested. `crawl` is
  intentionally not implemented.

## 5d. Issue triage (user review, P8)

**Done this pass (green):**
- **CRITICAL bug fixed** — a mutating action now records on the reference
  action chain (`core.doc.actions`), `doc.ref()` reflects the *current*
  chain, and `reload()` re-resolves that reference (replaying the chain) to
  reproduce state. Was: actions only emitted `ActionEvent`s, so `ref()`
  carried an empty chain and re-resolution lost the mutations.
- `Reference`: `fetch` removed (bloat); `resolve` is now a thin delegate to
  the bound client's `resolve` (or a lazy plan when unbound); `param` removed.
- `Document`: `navigate` dropped, `paginate` dropped, `save` removed, `query`
  removed (json is `select("a.b[0]")`), `replay` folded into `reload`;
  `links` returns a `Collection[Reference]`.
- `WebClientCore`: added the single `resolve(reference, *, browser, session,
  **opts)` decision point; removed `_navigate`, `_swap_document`, `_paginate`.
- `_LINK_ATTRS` removed; `Proxy` moved out of `document.py` to
  `core/webclient.py` (with the proxy pool).

**Done (P8 continued):**
- **WebClientCore = resolve + execute (async).** The user-facing builders
  `ref`/`fetch`/`search`/`summary` moved to the `WebClient` facade (sync
  bridges over the core's two async primitives); `execute` is now async on
  the core (`astream` for streaming), the facade bridges to sync. The core
  keeps the registry/session/scope machinery and the private `_fetch`/
  `_fetch_browser`/`_build_document` internals.
- **Document shims dropped.** The `_page`/`_lease`/`_routing`/`_attached`
  properties are gone; `WebClientCore` binds/reads `live._core` directly, so
  runtime state lives only on the `DocumentCore`.

**Done (P8 cont'd):**
- **Document ≠ Reference.** `Document` is now a sibling of `Reference` under
  `WebBase`: it holds the response (`url`/`final_url`/`content`/`status_code`/
  …) plus its own `actions`/`options` chain, and `ref()` returns the
  Reference it resolves from (registry lookup, else `Reference.from_url(url)`).
  Construction sites (`_build_document`, `_wrap_live_page`, element/binary)
  build from `url`, not the old `request_fields`. `model_post_init` sets
  ok/error from the status (0 = no response).

**Remaining (staged; each is a substantial, coupled change):**
- ✅ **Base types moved.** `core/ops.py` renamed to **`core/base.py`**, now
  the kernel: `@policy` + `WebBase`/`WebError`/`Field`/`Collection` +
  `NameScope`. `document.py` holds only `Reference`/`Document`/`Element` and
  re-exports the base types. `WebBase.reference/document(s)` decouple from
  the concrete classes via the `CLASSES` registry (no import cycle). The
  generated `Collection` stub now lives in `base.py`; `gen_stubs` targets it.
- ✅ **Representations via one `render(format)`.** `render` is the single
  representation function on `Document`, typed per format with overloads
  (like `attr`): `markdown`/`text`/`html` -> str, `elements` -> list[Element],
  `links` -> Collection[Reference]. The standalone `markdown`/`elements`/
  `links`/`data` are gone (they are formats now). Core rendering itself is
  now a **backing op**, not a plugin: the `render` implementation lives on
  `HtmlBacking`/`JsonBacking` in the new `core/backings/` package, and
  `Document.render` dispatches through `DocumentCore.dispatch("render", ...)`
  like every other op. `plugins/render.py` and `core_renderers()` are gone;
  `_render_table` now holds only user `wc.use(Renderer)` overrides, which a
  backing consults before its built-in. `text` decode moved onto
  `DocumentCore.text()`; `Document.text` is a thin delegator.
- ✅ **WebClientCore = resolve + execute only; one facade, three executions.**
  The ergonomic plan builders (`fetch`/`search`/`summary`) live on a shared,
  core-free `_Facade` (`webclient/client.py`): each builds a lazy `Plan` and
  hands it to `self._run`. `WebClient` runs it on a local core and blocks,
  `AsyncWebClient` awaits it (engine loop bridged to the caller's loop),
  `RemoteWebClient` submits it to `/execute`. `fetch` is now
  `ref.resolve(...)` through the same execute path (no `_fetch` bypass);
  `resolve` gained `optional=` so it is the complete fetch primitive. The
  core keeps `resolve`/`execute`/`astream` plus the registry/lifecycle it
  owns (`session`/`document`/`reference`/`use`/`release`); the facade
  forwards those. The service collapses to one path: a Document result of
  `/execute` is a remembered handle (`{"__doc__": meta}`) the remote client
  rehydrates, so the per-op `/fetch` and `/search` endpoints are gone.
- ✅ **Remote = a lazy plan submitter; session-based API.** `RemoteDocument`
  builds a `Plan` rooted at its server-side document (the `doc` lazy root)
  and submits it to `/execute`. `WebSession` gained `fetch`/`execute`/
  `search`; the service routes each request through a session and exposes
  `/fetch`, `/execute` (rooted at a URL or a `document_id`), `/search`,
  `/crawl` (501), `/document/{id}`, `/sessions`, `/events`. The per-op
  `/documents/{id}/render` and `/select` endpoints are gone.
- ✅ **`model_post_init` removed** (folded in): `WebClientCore` derives
  ok/error at build via `apply_status(doc)`.

**The staged review list (PLAN §5d) is complete.**

## 6. Risks / watch items

- **Scope creep back into a rewrite.** Measured, not argued: the green-tree
  rule and the budget table. A PR that adds a file must delete one.
- **Checker divergence.** Already bit once (`bind_ops`). Both checkers on the
  corpus in CI; any idiom that passes only one is not an idiom we use.
- **Stub drift.** The `Collection` block is generated; the drift test is
  the guard. Hand edits between the markers are rejected in review.
- **Root names shadow user locals (found at P3). Decided: keep `doc`/
  `many`/`ref`; don't name locals after them.** `demo.py` was renamed
  (`shop`, `spec`, `remote_doc`); examples and docs follow the same rule.
- **Non-op methods on lazy objects.** Reading a request/response field on a
  lazy object (directly or via `url`) raises naming the fix. A non-op method
  that only touches defaulted fields (`ref.with_params(...)`) is not caught
  and returns an eager object; catching it needs a per-attribute hook on
  every object. Accepted for now.
- **`extract` merge + in-place mutation.** A Document shared across two eager
  plans accumulates both sets of fields — scope `_fields` per plan-context or
  document the sharing; test it.
- **Action-chain replay drift.** Best-effort by decision; the failure must be
  loud — assert an action on a changed page raises.
- **Name-registry growth.** Only three kinds register; retention tied to
  session/ttl; a long-lived client with no sessions caps names — leak test.
- **Bridge deadlock.** Sync facade from a running loop raises, pointing at
  `.core`.
- **`LiveDocument` keeps its own pre-P4 `select`/`attr`** (with
  `optional=`), overriding the ops. A plan run over a `LiveDocument`
  dispatches `Document.select` from the registry, i.e. the load snapshot,
  not the live DOM. P4 removes the override.

## 7. Open decision at P3: the budget

The ceiling (≤ 3 871 at P6) will be missed by ~150–250 lines with the scope
as written. The remaining duplication is public API that the new surface
makes redundant, so hitting the ceiling means removing it. Options:

1. **Remove the redundant surface (recommended).** Each item is a second
   way to do something the ops already do (sizes measured at P3):
   - typed views `HTMLDocument`/`JSONDocument`/`XMLDocument`/`BinaryDocument`
     and `_view` (81 lines, ~75 net): `select`/`attr` cover `json.query`
     and xml; `title`/`links`/`markdown` become render formats; `save`
     becomes a one-line `Document` method.
   - `RemoteRef`/`RemoteDocument` imperative surface (81 lines, ~70 net):
     remote becomes plans plus document handles.
   - static pagination (`Document.paginate` 18 + `client._paginate` 56,
     already listed as deferred in §3) re-expressed as a plan over
     `select("a.next").attr("href").resolve()` (~40 net).
   Projected P6 ≈ 3 860, at the ceiling with no slack. (`optional=` removal
   is already counted in the P5 re-projection.)
2. **Re-baseline the ceiling** to ~4 100 on the grounds that the old tree
   had none of: error policy, addressable names, safe dispatch, typed
   Field/Collection, the stub generator. Keeps every existing API.
3. **Treat the miss as the signal and stop the refactor** at P3. Not
   recommended: P2/P3 already removed the two-implementation lazy layer and
   its bugs (nested `map` drop, leaked Expr args, generic `getattr`
   dispatch), and reverting loses that.

P4 does not start until this is decided.

## 7. Async-core migration (2026-09-11, in progress)

**Goal.** The core is async-native: no `_ensure_loop().run(...)` bridges live
inside it. The *sync* `WebClient` is the sole async→sync bridge (its `_run`
drives the engine loop); `AsyncWebClient` awaits the core natively on the
caller's loop; a `RemoteWebClientCore` speaks plans over HTTP, and a remote
document is a *bound lazy expression* (no Document crosses the wire). This
folds together the three requests: async-by-default, remote-as-a-core, and a
thin front-end. Each stage keeps the suite green.

**Bridges to remove (audited).** `core/webclient.py` close/release/teardown
(`self._loop.run`); `pool.acquire`/`release` sync wrappers; `DocumentCore
.parsed()` live re-snapshot and `identity_path()` evaluate; `@policy._settle`
off-loop bridge (this one stays -- it is the sanctioned settle point, now
reached only via the sync facade).

- ✅ **A. Async lifecycle.** `WebClientCore.aclose` + `_ateardown_session`
  (await, no inline `loop.run`); sync `close` is a one-line bridge. Green.
- ✅ **B. Live snapshot off the read path.** `DocumentCore.asnapshot()` (async)
  refreshes content; `parsed()` reads cached content; live `render` awaits a
  fresh snapshot via `HtmlBacking._live_render` (settled by `@policy`, which
  `render` now carries). `identity_path` is captured at selection time (folded
  into `LiveSelect`'s `outerHTML` evaluate) and cached -- no bridge. Green.
- ✅ **D. Invert the facades; async by default.** `@policy._settle` now returns
  the awaitable whenever a loop is running in this thread (the executor OR an
  async caller), else bridges onto the engine loop -- so an async caller runs
  the core natively. `AsyncWebClient._run` awaits `core.execute`/`astream` on
  the caller's loop (no engine thread); `WebClient._run` is the sole engine-loop
  bridge. Deleted `EngineLoop.submit`/`aiter` + the `wrap_future` path. Green.
- ✅ **C. Async pool surface.** The sync `pool.acquire`/`release` wrappers and
  `Lease.release()` were dead (only `_acquire`/`_release` are used); deleted.
  `pool.stats()` stays. No sync bridge remains in the pool.
- ✅ **E. Remote is a core backend.** `WebClient`/
  `AsyncWebClient` wrap *any* core exposing the common interface
  (`resolve`/`execute`/`astream`/`aclose`/`_ensure_loop`/`session`); remote is
  `WebClient(core=RemoteWebClientCore(url, token))`. The core's `execute` runs
  the plan remotely (POST `/execute`) instead of locally. Documents and
  References are already the lazy interface, so there are NO `RemoteDocument`/
  `RemoteRef`/`RemoteSession`: `core.resolve(ref)` returns a shallow lazy
  Document handle (meta + a bound lazy root via a document-source plan,
  `Plan(root="Document", source={"document_id": id})`), and `Session` is
  backend-agnostic over the core. Executor `_start` + the `/execute` endpoint
  learn the document source. Rewrite remote tests to `WebClient(core=...)` +
  deferred/batched `execute`. (~ -80)
- ✅ **F. Front-end collapsed.** Absorbed by A–E: the op surface is the
  decorator-dispatched plan builders on `_Facade`; each concrete client is one
  `_run` over `_Client` (core ownership + lifecycle + `__getattr__` forwarding);
  the old three-class ladder and the duplicated `RemoteWebClient` facade are
  gone. Architecture.md updated.
- ✅ **Remote moved into `core/`; shared `EngineCore` base.** `RemoteWebClientCore`
  now lives in `webclient/core/remote.py` beside `WebClientCore`, and both
  inherit `EngineCore` (`webclient/core/base.py`) for the loop lifecycle
  (`_ensure_loop`/`close`/`__enter__`/`__exit__` + an async `aclose` and a
  `_finalize` hook). `webclient/remote.py` is deleted; `RemoteWebClient`/
  `RemoteWebClientCore`/`RemoteError` re-export from `webclient` unchanged.

The async-core migration is complete: one async core with a shared base and
local/remote backends, one `Document`/`Reference`/`Session`, a thin sync
client and a native async client.

## 8. Full-lazy surface migration (planned)

**Reference shape:** `example.py` (a runnable design sketch of the target
surface). This section turns it into a staged migration.

**What it is, honestly:** a *rewrite of the surface layer and the typing
corpus* -- not a refactor. Nearly every test changes. Per
[[refactor-not-rewrite]] I flag it as rewrite-shaped; the user has directed it
across many turns. The green-tree rule still holds: each stage below ends with
a green tree (tests rewritten *within* the stage that flips them), no long red
period.

**Key enabler (verified):** the executor drives real *engine* objects with
`getattr(value, op)(...)` (`webclient/lazy/executor.py`), and eager already
runs through that engine. So the engine -- `Document`/`Reference`/`Collection`/
`Field` + `@policy` + backings + executor -- **stays**. The migration makes the
*surface* always lazy (a generic `Expr`) and adds a `collect()` layer that runs
the engine and materialises the result. `Expr` is already model-independent
([[expr-independent-of-models]]) -- that invariant is preserved, not rebuilt.

**Settled decisions (from the design turns):**
- Everything on the surface is a generic `Expr`; `collect()` is the single
  evaluation trigger; nothing runs until then.
- Two-tier typing: `LazyDocument` (records; `collect() -> Document`) and a
  materialised `Document` (response data + still-chainable lazy methods).
  `LazyCollection[LazyT, T]` (`collect() -> list[T]`). Runtime is one `Expr`;
  the tiers are typing shims via `lazy(cls)`.
- Eager is **per-call `_collect=True`**, typed by a generated overload that
  returns the materialised tier directly (verified: pyright + mypy resolve it).
  No separate `Eager*` type family.
- Branching is Polars-style free `when(cond).then(a).otherwise(b)` (off `Field`);
  `filter` is a free function / lazy op (off `Collection`).
- `Field` kept only as an opt-in `.field()` envelope; default `collect()`
  returns plain Python.
- Three addressable roots, symmetric: `document`/`reference`/`session` --
  bare (unbound/context), `x(id)` (by stable id), `reference(url, ...)`
  (construct). A resolved document's only structural metadata is its id.
- Remote stays a core backend; **eager remote uses a websocket** so each op can
  round-trip and keep server-side state (eager parity with local); lazy remote
  stays one batched `Plan` over HTTP `/execute`.

**Stages (each ends green):**

- **1. Typed shims + generator (additive).** Author the target typing corpus
  (`Lazy*`/materialised/`LazyCollection[LazyT, T]`/roots/`when`/`filter`/opt-in
  `Field`). Extend `scripts/gen_stubs.py` to emit, from one op-signature source,
  both the lazy shims and the `_collect=True` overload pairs (the same generator
  that already emits the `Collection` twin). No runtime change; new corpus
  passes both checkers; old suite untouched.
- **2. `collect()` + lazy entry points (the flip).** `wc.fetch`, the roots
  `document`/`reference`/`session`, and `wc.execute` return lazy `Expr`s (typed
  as the shims). Add `collect()`: run the plan through the existing engine, then
  materialise -- a document result becomes the materialised `Document` (data +
  lazy chaining rooted at its id), a scalar becomes the value, a collection a
  list. Rewrite the suite to the lazy surface. **The large stage;** ends green
  on the new surface.
- **3. Branching / filter / Field surface.** Add free `when(...)` and `filter`;
  make `Field` opt-in via `.field()` (default collect returns plain values).
  Remove `Field.when/then/otherwise` and `Collection.filter` as the surface.
  Update tests. Green.
- **4. Internalise the eager engine classes.** The eager `@policy` methods on
  `Document`/`Reference`/`Collection` are now executor-only. Make the public
  `Document`/`Reference`/`Collection`/`Session` names the typing shims; the
  runtime engine classes become internal. Delete any eager surface no longer
  reachable. Measure the budget (surface shrinks; generated stubs grow -- net
  to be measured).
- **5. Eager execution paths.** Per-call `_collect=True` routes through
  `collect()` (one path). For a remote core, an eager op opens/uses a websocket
  session and evaluates op-by-op server-side (state preserved between ops);
  lazy remote keeps the single batched `/execute`. Same surface either way.
- **6. Docs + demo + gates.** Update Architecture.md and `demo.py` to the lazy
  surface; final full suite + both checkers + stub check + demo.

**Open items / watch:**
- `attr`'s link overload overlaps the general one -> `# type:
  ignore[overload-overlap]` (as today).
- `project()` stays lazy: it returns a lazy rows expr that `collect()`s to
  `list[dict]`, not a `list` directly (the sketch's return type there is a
  rough edge).
- Budget: measure at stage 4; the surface should shrink, generated stubs grow.
- Websocket eager remote (stage 5) is the most novel piece; keep it behind the
  same core interface so lazy remote and local are unaffected.

**Progress:** the `collect()` trigger landed first (additive) --
`Expr.collect(context=None)` runs a recorded plan on the bound client's core
(or the process default) via the engine loop; `collect` is a reserved,
non-recordable name. `Reference(url).resolve().select(...).attr(...).collect()`
now works alongside `wc.execute`. Then `wc.lazy(url)` -- a lazy reference root bound to THIS client (not the process default), the companion to collect() for non-default clients. Then Polars-style free when(cond).then(a).otherwise(b) (a new "when" plan step) and filter(coll, pred), exported from webclient. Additive cornerstones done; addressable roots + two-tier typing await the entry-point flip. 151 tests green.

**Flip progress (stage 2):** the ergonomic helpers are now lazy --
`fetch`/`search`/`summary` return expressions (via a shared `_rooted`), run
with `.collect()` / `await ac.execute(...)` / `wc.execute`. The dead `plan_op`
decorator and `summary_expr` were removed. Still eager (the entangled
remainder): `wc.ref`/`Reference.resolve`/`doc.ref()`/execute-context (37+
multi-role sites: context, construction, inspection, resolve) and
`Session.fetch`; plus the addressable `document`/`reference`/`session` roots
and two-tier typing. 151 tests green.

**Flip progress (stage 2, cont.):** `wc.ref(url)` is now a lazy reference root
(was eager) -- `wc.ref(url).resolve()` records and runs on `.collect()`. The
engine stays eager: it builds its own real References internally, and a lazy
reference used as an execute context is unwrapped back to a real Reference
(`_start`; the remote core unwraps to a url). Error policy split: the top level
raises (failed resolve/fetch), while `extract`/`filter` evaluate sub-expressions
under RETURN and fan-out stays resilient. So the ergonomic surface
(`fetch`/`search`/`summary`/`ref`) is fully lazy; within a *materialised*
(collected) Document, chaining stays eager. Remaining: `doc.ref()`/`reload`,
`Session.fetch`/`ref`, the addressable `document`/`reference`/`session` roots,
and two-tier typing. 151 tests green.
