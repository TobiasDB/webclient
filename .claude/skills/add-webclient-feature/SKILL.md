---
name: add-webclient-feature
description: How to add a feature (an op, a Backing, a value model) or a transport client to this web-client repo. Use when implementing anything user-facing on WebClient/Document/Reference/Session, adding a new op to an existing surface, or adding a new transport (http/browser/etc.).
---

# Adding a feature or client to web-client

## The architecture in one breath

Every core (`WebClient`, `Document`, `Reference`, `Session`) is a `WebCore`: it holds
**Backings**, CHOOSES which apply to its current state, and DISPATCHES an op to the
first chosen backing that `provides` it. **The core IS the surface** — the class is
literally named `Document`/`Reference`/`WebClient`/`Session` (no `Core` suffix). Each
inherits a generated `I<Core>` interface (fields + typed ops) from its `models.py`, so
its ops are statically visible on the core itself; the `Lazy*`/`Async*` views are
generated too (`scripts/gen_stubs.py`), never hand-written. Sync/async/remote are
**dispatch modes** (`_mode` on the client), not subclasses — one op definition lights
up all of them.

So a feature is almost always: **a Backing (ops) + optionally a value model. That's it.**
You should touch **zero** dispatch/machinery code.

Key files:
- `webclient/core/web_core.py` — `WebCore` + `Backing` base (the dispatch core; don't edit for a feature).
- `webclient/core/<kind>/` — one package per core; one backing per module.
- `webclient/models.py` — the cross-cutting **Event** taxonomy (pydantic, no cores). A core's own value models + its `I<Core>` interface live in that core's `models.py` (e.g. `core/document/models.py`: `IDocument`, `Element`, `Summary` facets).
- `webclient/clients/` — the transport layer (httpx/browser/pool); the ONLY place touching httpx/playwright.
- `scripts/gen_stubs.py` — generates every typed surface from the backings.
- `webclient/surfaces/eager.py` / `lazy.py` — the generated stubs (op bodies are generated; the `TYPE_CHECKING` import headers are hand-maintained).

## Anatomy of a Backing

```python
class MyBacking(Backing):
    provides = frozenset({"my_op"})     # call ops: obj.my_op(...)
    props    = frozenset({"my_prop"})   # property ops: obj.my_prop
    io       = frozenset({"my_op"})     # ops that cross the IO bridge (see below)
    gate     = "ok"                     # capability granted when chosen
    page_scripts = ()                   # browser scripts to install on live pages
    def applies(self, core): return True   # is this backing in play for this core's state?
    def on_load(self, core, result): ...   # hook: a live browser page finished loading

    def my_op(self, core, arg, *, kw=None) -> "SomeType":
        ...   # `core` is the receiver; return a real core/value/list
```

- **`io` ops are `async def`** and return the surface type (e.g. `-> "Document"`).
  `dispatch` bridges the coroutine onto the right mode (sync blocks, async hands back
  the awaitable, remote round-trips). **Backings never call `bridge`/`cast` themselves.**
- **Non-io ops are plain `def`**, in-memory, synchronous.
- The op's typed signature (params + return annotation) is the single source of truth the
  generator reads. Return `"Document"`/`"Reference"` (the core classes),
  a pydantic model (a terminal value), `list[...]` (→ `Collection`/`list`), or a scalar.

## Recipe: add an op to an existing surface

1. **Write the op** on the relevant backing module (or a new `MyBacking`). Match the
   surrounding style; annotate params and the return type — the generator needs them.
2. If it does IO (fetch, network, browser), add its name to `io` and make it `async def`.
3. If you added a *new* backing class, register it in that core's `BACKINGS` tuple
   (e.g. `WebClient.BACKINGS = (FetchBacking(), SearchBacking(), MyBacking())`).
   `BACKINGS` is data; sessions inherit the client's.
4. **A backing composing another op calls it directly and typed** — `doc.select(...)`,
   `ref.url` — because every core *implements* its ops via a generated interface it
   inherits (`IDocument` / `IReference` / `IWebClient`, in the core's own `models.py`;
   see `SearchBacking.search`). No `dispatch("...")` strings, no casts.
5. Regenerate: `env/bin/python scripts/gen_stubs.py`. It emits the op onto eager/async/
   lazy tiers automatically.

## Recipe: add a value model (structured result)

1. Add the pydantic model to **the backing's own core package `models.py`** —
   `core/document/models.py` (document backings: `Element`, `Summary` facets),
   `core/client/models.py` (client backings: `SearchResult`) — with an `__all__`
   entry. Keep it pure data. (The top-level `webclient/models.py` is now only the
   cross-cutting **Event** taxonomy — don't add core-specific models there.)
2. Export it for users: re-export from `webclient/__init__.py` (and, for document
   value types, from `webclient/summary.py`) so the public import paths keep working.
3. **Import it at module level in the backing** that returns it (`from .models import
   MyModel`) — the generator resolves return annotations from the backing's globals.
4. Add its name to the `TYPE_CHECKING` import header of `surfaces/eager.py` AND
   `surfaces/lazy.py` (these headers are hand-maintained; the emitted `list[MyModel]`
   must resolve there).

## Recipe: add a transport client

1. Add a `Client` + `ClientFactory` in `webclient/clients/` (see `http.py`, `browser.py`).
   The client owns its protocol end-to-end (request + response interpretation).
2. Register its factory in `WebClient._init_transport`'s `ClientPool({...})`.
3. A backing leases it: `async with await core.pool.lease("mykind") as lease: ... lease.client...`.
   Don't loop back out through another op to reach transport.

## The gate — must stay green (run all five)

```
env/bin/python scripts/gen_stubs.py --check    # generated stubs match the backings
env/bin/mypy --strict webclient
env/bin/pyright webclient
env/bin/python -m pytest -q                     # keep every test; add one per feature
env/bin/python demo.py                          # demo.py showcases every feature; extend it
```

Tests use `pytest-httpserver` (the `httpserver` fixture) — serve canned pages locally,
never hit the network. Async tests use an inner `async def main()` + `asyncio.run(main())`
(no `pytest.mark.asyncio`). `demo.py` serves its own offline site via a `Handler`.

## Gotchas

- **Don't hand-edit generated op bodies** in `surfaces/*.py` — regenerate. You *do* hand-edit
  the `TYPE_CHECKING` import headers there when adding a new value-model name.
- **`io` matters**: forgetting it means the async surface won't type the op `async` and
  the sync client won't bridge it correctly.
- **`applies`** must be cheap and defensive — it is probed against every core the client
  owns. Gate on state the core actually has (e.g. `core._page is not None`).
- Value models live in a `models.py` (per-core-package for a core's own models; the
  top-level one for the cross-cutting event taxonomy) — pure data, no cycle risk. Each
  core's `models.py` also holds its `I<Core>` interface (fields + ops); see below.
- **The core-implements-its-interface pattern** (all four cores): each core inherits a
  generated `I<Core>(BaseModel)` in its `models.py` — the Core Fields + the eager ops
  (ops typed under `TYPE_CHECKING`, so at runtime it's just the data model and
  `__getattr__` still dispatches). The class is named for the surface (`Document`, not
  `DocumentCore`). The generator emits the interface region into that `models.py` via the
  `eager` tier + the `HAS_INTERFACE` set (async IO ops get an `[override]` ignore;
  `Collection` is covariant so async `Collection[...]` returns override cleanly), and the
  `models.py` module is in the mypy stub-override list.
- Commit trailers for this repo: `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`
  and the `Claude-Session:` line. Never commit `docs/*.md` (gitignored).
