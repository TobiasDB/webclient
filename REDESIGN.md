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

1. **One class, two evaluation modes.** `then` / `map` / `otherwise` /
   `filter` are methods on the real classes; the module roots `doc`, `el`,
   `ref` are those same classes with `is_lazy=True`. Eager calls evaluate,
   lazy calls record, through one implementation. **No generated twin, no
   `.pyi`, no codegen** — eager and lazy cannot drift because they are
   literally the same method. `@op` still declares purity, capability,
   resource and cardinality as metadata the scheduler and validator read.
2. **Ops return `Value[T]` / `Selection[T]`.** A wrapper is materialised
   eagerly and unevaluated lazily, so `.alias()`/`.otherwise()`/`.map()` are
   type-visible in both modes. `.get()` unwraps on the eager path. Declaring
   raw returns (`attr -> str`) would make `.alias()` a type error on `str` in
   the language's most common idiom.
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
9. **Two record constructors, one error combinator.** `then(...)` is one
   context → one record; `map(...)` is `then` lifted over a collection. Both
   take positional expressions named by `.alias()` and keyword expressions.
   `otherwise(...)` takes either a **recovery projection** or a sentinel
   (`RAISE_ERROR` / `DROP_ROW` / `NULL`), absorbing what was `on_error` plus
   `require` plus plan-level strictness. Recovery blocks see two roots — `doc`
   (context reached at failure, possibly synthetic) and `err` (the failure) —
   and produce **tagged** records carrying `ok`. Results are trees, not
   tables; `.explode(path)` flattens for tabular consumers. A resolved
   document is the *context* of the `then` that projects it, so Documents
   never need parking in columns.
9b. **Documents accumulate extractions.** An eager `then` stores its record on
   the document (`doc.fields`), and `field(name)` means one thing in both
   modes: a value already extracted in this context — the record being built
   in a plan, `doc.fields` on a Document.
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
  values.py        Value[T], Selection[T], Expr — the wrapper types
  records.py       Record, RecordSet, Err
  plan.py          the typed plan IR
  execute.py       the evaluator
  explain.py       plan.explain()
  roots.py         doc / el / ref / err / field
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
scheduler; `cardinality` is what recording and evaluation both read, so
element-wise mapping is one declared fact rather than two assumptions. The
same decorated method serves both modes — it branches on `self.is_lazy` once,
at the top, and everything after that is the single implementation.

### Lazy IR

```
Plan   = { version, source, steps }
Source = Context | Reference(url) | Document(id)
Step   = Call(op, args) | Then(fields) | Map(fields) | Filter(plan)
       | Otherwise(recovery | sentinel) | Explode(path) | Limit(n)
Field  = { name, plan }        # from .alias() or a keyword
Arg    = Literal(v) | FieldRef(name) | Sub(Plan)
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

## 4. Milestones — **all delivered**

Each shipped importable, tested, mypy-clean, with `demo.py` extended to
exercise everything landed so far (21 sections, all runnable).

- **R0 — walking skeleton.** ✅ `ops.py` + registry + capability errors;
  `Reference`; `Backing` protocol with `Static` + `Http`; `Document` with
  `SelectSurface` only; `Element` addressing; `pool.py` http leases;
  `WebClient.resolve`; `Value`/`Selection`; sync facade + `.core`.
  Fix `pyproject` packages.
  *Gate:* `EXAMPLES.md` §1 runs verbatim; a browser op raises
  `UnsupportedOperation`; redirect link resolution is correct;
  `mypy --strict` clean including `.alias()` on an `attr()` result.
- **R1 — the expression language.** ✅ `is_lazy` recording on the real classes,
  `then`/`map`/`otherwise`/`filter`/`alias`/`explode`, `doc.fields` +
  `field()`, IR, record-time validation, DAG compile, evaluator over the same
  op implementations, `explain`, JSON round-trip.
  *Gate:* differential corpus — every example plan run eagerly and lazily,
  identical results; a test asserting every IR node kind is implemented or
  raises; nested-map correctness; tagged-recovery shapes; bounded-fan-out
  memory test.
- **R2 — render.** ✅ Registry + `markdown`/`readable`/`links`/`elements`
  ported from the current renderers. *Gate:* golden files, plugin format
  registration.
- **R3 — browser backing.** ✅ `PageBacking`, page leases, context per
  session, `InteractSurface`, `DomSurface` (`wait_stable`, `wait_for`,
  `evaluate`, `screenshot`), node-id element addressing, `navigate` +
  snapshot-readable staleness. *Gate:* `EXAMPLES.md` §2 and §9 verbatim;
  **the same plan produces identical rows over http and page backings**;
  no lease leaks under `pool.stats()`.
- **R4 — telemetry.** ✅ Records written by both backings; observer registry.
  *Gate:* redirect chain, console lines, request timings; no subscription
  ordering anywhere in the code.
- **R5 — sessions, middleware, pagination.** ✅ `Session` lifecycle and
  storage_state; retry/proxy/cache as ordered `Middleware`; `paginate` as a
  plan construct. *Gate:* login flow in §2; retry policy unit-tested
  without network.
- **R6 — cut over.** ✅ Delete superseded modules, port `demo.py` fully,
  replace root `models.py` with the new frozen interface, merge.

## 5. Deferred

Service/HTTP API, remote client and `RemoteBacking`, rrweb, DOM streaming
and replay, CDP passthrough, pagination resume/prefetch, full retry policy
(5xx/429/Retry-After/jitter), `scrape()` one-shot convenience.

## 6. What changed during the build

Recorded because each was a real decision, not a detail:

- **`links` is a render format, not its own op.** It was going to be an op on
  the grounds that it returns References rather than text — but a format
  already returns whatever it means (`elements` returns typed blocks), so a
  second name could not be justified.
- **`attr("href")` returns a `Reference`, not `Value[Reference]`.** The op's
  result class depends on its argument (`returns_for` on the spec), which
  makes `attr("href").resolve()` a complete thought in both modes and one
  that type-checks.
- **Lazy wrappers forward unknown names to the op registry.** `field("link")`
  must accept `.resolve()`, and a column's type is a run-time fact — so
  `field()` is typed `Any` and lazy `Value`/`Selection` forward by name,
  validating against the registry as they record. A typo fails at authoring
  time with the list of known ops, which is the guarantee the old design's
  record-time validation gave.
- **`FieldStep` was deleted from the IR.** `field` is an op, so a second
  node kind for it would have been a node the evaluator had to special-case —
  exactly the sort of thing that used to get silently skipped.
- **Renderers fall back for inline-only documents.** `markdown` and
  `elements` returned empty for a document with no block elements, which is
  technically true and practically useless.

## 7. Open

- Whether a remote client supports imperative ops at all, or only plans and
  renders (`EXAMPLES.md` §12). Deferred with remote; the address-based
  Element and serialisable IR keep both doors open.
- A real retry policy (5xx / 429 / `Retry-After` / backoff + jitter). The
  middleware seam is in place; the policy is deliberately not grown by
  accident.
