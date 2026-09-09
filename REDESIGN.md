# Redesign: core Document + WebClient

Supersedes `PLAN.md` (M1–M7) for the core. `EXAMPLES.md` is the interface
spec — the design is judged by how those read, and every milestone below
must keep them literally true.

## 1. Why

Evidence gathered against the current tree, not impressions:

| Problem | Evidence |
|---|---|
| Expression engine silently wrong | `map().map()` drops the second map; `filter(field==field)` compares against `None` and returns `[]`; an Expr as a call argument leaks `{"__expr__":…}` into the callee |
| Recorder and executor are two implementations of one language | `.attr("text")` over a collection validates at record time, raises `'list' object has no attribute …` at run time |
| `compile()` is decorative | `ExecutionGraph` is built with deps/resource/page_group, then `_arun` discards it and re-walks `plan.steps` linearly |
| Unbounded fan-out | `as_completed([build(e) for e in elements])`; one failing row orphans the rest |
| `Document(Reference)` is a modelling error | link resolution uses the *request* ref, not `final_url` — a link `c.html` on `/a`→302→`/deep/b` resolves to `/c.html` |
| Document is sharded | 4 view aliases via `model_construct` + shared `__pydantic_private__` (undocumented pydantic behaviour, ISSUES #6), plus `Node`/`LiveNode`/`LiveDocument` |
| `WebClient` is a god object | 555 lines: fetch, page setup, script injection, cookie priming, subscription ordering, doc construction, navigation, pagination, teardown, executor |
| Events conflate four jobs | capture + transport + store + extension point; `_wrap_live_page` must subscribe→adopt→cancel→resubscribe to not lose events during `goto` |
| Plugins are untyped | `surfaces: list[str]` + `raw: Any`; `Renderer` is a `Plugin` that never attaches; `attach_async` bolted on |
| Tests are milestone-shaped | 134 green while the above is true |
| Wheel is broken | `packages = ["webclient"]` omits every subpackage |

## 2. Decisions

Settled in design review; revisit only with cause.

1. **One op declaration, two bindings.** `@op` records name, typed signature,
   return kind, purity, capability and resource once. Eager surfaces bind to
   implementations; `LazyDocument`/`LazyElement` are *generated* recorders
   over the same registry, with a generated `.pyi` checked in CI. The plan
   evaluator calls the same bound implementations — there is no second
   `select`. This is the structural fix for the divergence class of bug.
2. **One `Document`, surfaces as mixins, capability from the backing.**
   `Document(SelectSurface, InteractSurface, DomSurface, RenderSurface)`.
   Ops are flat. An op the backing can't serve raises `UnsupportedOperation`
   naming the fix. No `LiveDocument`, no typed view aliases.
3. **Capability is runtime state**, not fixed at resolve time (§2 of
   `EXAMPLES.md`): a navigated-away Document keeps its static half and loses
   its live half. Checks read the backing, every time.
4. **One public interface, sync-reading.** No `webclient.aio`. The engine is
   async; `.core` on client and document is a generated async pass-through
   for callers already in a loop. Sync-from-loop raises, pointing at `.core`.
5. **`Element` is an address**, resolved handle cached. Serialisable, so the
   same Element crosses plans and any future wire.
6. **`resolve` is the only verb** for Reference → Document. `fetch` is gone.
7. **`attr(name)` is the only accessor** — real attributes and pseudo
   (`text`, `html`, `title`). No `.text`/`.html` properties. Likewise
   `render(format)` is the only render op. One op, one IR node, one stub
   signature, overloads narrowing returns.
8. **Two lazy roots**: `doc` (the document), `el` (current element in `map`).
9. **Error model**: `.on_error("null"|"drop"|"fail")` step-level, default
   `fail`; `.require(*fields)` row-level. Documents may occupy intermediate
   columns; `collect()` drops non-scalar columns unless `keep=` names them.
10. **No event bus.** `doc.telemetry` is a typed record written directly by
    the backing; `wc.on(RecordType, handler)` dispatches by class for
    streaming consumers.
11. **No `Plugin` base class.** Three narrow typed extension points:
    renderer registry `(kind, format) -> fn`, request `Middleware`, and
    `PageScript`.
12. **Rewrite in place** on `redesign`; old modules deleted as superseded.

## 3. Layout

```
webclient/
  ops.py           @op, OpSpec, registry, capability + resource tags
  reference.py     Reference — request spec only
  document.py      Document (surface composition), Element, StaleDocument
  surfaces/        select.py  interact.py  dom.py  render.py
  backing.py       Backing protocol; HttpBacking | PageBacking | StaticBacking
  client.py        WebClient: session, pool, resolve(), registry, .core
  session.py       Session — cookies, storage_state, proxy, ttl
  pool.py          Pool[T], http + page factories, leases
  telemetry.py     typed records, observer registry
  render/          registry + core renderers
  lazy/            ir.py · record.py · _generated.pyi · plan.py · eval.py
  sync.py          generated sync facade (anyio blocking portal)
```

`engine/`, `events.py`, `plugins/`, `live.py`, `remote.py` all disappear.

### Resolve — the one lifecycle decision point

```python
async def resolve(self, ref, *, browser=False, session=None) -> Document:
    session = session or ref.session or self.default_session
    backing = PageBacking if browser else HttpBacking
    lease   = await self.pool.acquire(backing.resource, session)
    state   = await backing.open(ref, session, lease)   # middleware: retry, proxy
    return self._register(Document(request=ref, backing=state))
```

A Document holds a Backing and nothing else from the client. `final_url`
lives on the Document and is what link resolution uses.

### The op contract

```python
class SelectSurface:
    @op(pure=True, returns=Element, cardinality="one->one")
    def select(self, sel: str, *, index: int = 0, optional: bool = False) -> Element: ...

class InteractSurface:
    @op(capability="browser", resource="page", mutates=True, returns=None)
    def click(self, sel: str, *, timeout: float | None = None) -> None: ...
```

`capability` drives `UnsupportedOperation`; `pure`/`resource` drive the
scheduler; `cardinality` is what the recorder and evaluator both read, so
element-wise mapping is one declared fact rather than two assumptions;
the signature drives record-time validation and the generated stub.

### Lazy IR

```
Plan   = { version, source, steps }
Source = Context | Reference(url) | Document(id)
Step   = Call(op, args) | Map(fields) | Filter(plan) | Require(fields)
       | OnError(policy) | Explode(field) | Limit(n)
Arg    = Literal(v) | Field(name) | Sub(Plan)
```

Typed pydantic unions throughout — an Expr as an argument is `Sub`, a
first-class case, not a leaked dict. Rules the evaluator enforces:

- Every step declares input/output cardinality; a mismatch is a **compile
  error**, never a runtime surprise.
- An unknown step kind is a **compile error**. There is no branch that
  returns the row unchanged.
- Nested `Map` produces a list column; `explode()` is the only flattening.
- Fan-out is a bounded work queue, not a materialised task list; a failing
  row cancels its siblings deterministically and orphans nothing.

## 4. Milestones

Each ships importable, tested, `mypy --strict` clean on new modules, with
`demo.py` extended to exercise everything landed so far.

- **R0 — walking skeleton.** `ops.py` + registry + capability errors;
  `Reference`; `Backing` protocol with `Static` + `Http`; `Document` with
  `SelectSurface` only; `Element` addressing; `pool.py` http leases;
  `WebClient.resolve`; sync facade + `.core`; stub generation + CI check.
  Fix `pyproject` packages.
  *Gate:* `EXAMPLES.md` §1 runs verbatim; a browser op raises
  `UnsupportedOperation`; redirect link resolution is correct; generated
  stub matches the registry.
- **R1 — lazy core.** IR, generated `LazyDocument`/`LazyElement`,
  record-time validation, DAG compile, evaluator over the same op impls,
  `map`/`filter`/`require`/`on_error`/`explode`, `explain`, JSON round-trip.
  *Gate:* differential corpus — every example plan run eagerly and lazily,
  identical results; a test asserting every IR node kind is implemented or
  raises; nested-map correctness; bounded-fan-out memory test.
- **R2 — render.** Registry + `markdown`/`readable`/`links`/`elements`
  ported from the current renderers. *Gate:* golden files, plugin format
  registration.
- **R3 — browser backing.** `PageBacking`, page leases, context per
  session, `InteractSurface`, `DomSurface` (`wait_stable`, `wait_for`,
  `evaluate`, `screenshot`), node-id element addressing, `navigate` +
  snapshot-readable staleness. *Gate:* `EXAMPLES.md` §2 and §9 verbatim;
  **the same plan produces identical rows over http and page backings**;
  no lease leaks under `pool.stats()`.
- **R4 — telemetry.** Records written by both backings; observer registry.
  *Gate:* redirect chain, console lines, request timings; no subscription
  ordering anywhere in the code.
- **R5 — sessions, middleware, pagination.** `Session` lifecycle and
  storage_state; retry/proxy/cache as ordered `Middleware`; `paginate` as a
  plan construct. *Gate:* login flow in §2; retry policy unit-tested
  without network.
- **R6 — cut over.** Delete superseded modules, port `demo.py` fully,
  replace root `models.py` with the new frozen interface, merge.

## 5. Deferred

Service/HTTP API, remote client and `RemoteBacking`, rrweb, DOM streaming
and replay, CDP passthrough, pagination resume/prefetch, full retry policy
(5xx/429/Retry-After/jitter), `scrape()` one-shot convenience.

## 6. Open

- Whether a remote client supports imperative ops at all, or only plans and
  renders (`EXAMPLES.md` §12). Deferred with remote; the address-based
  Element and serialisable IR keep both doors open.
