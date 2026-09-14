# Why three surface/model layers — and how far they can collapse

*Status: design note (analysis only). Answers: why `surface.py` + `surfaces.py` +
`models.py`, and can they become "simple generated models" like the lazy tier?*

## TL;DR

The three layers are **not** incidental duplication. They split along one real
axis: **eager values have a runtime; lazy values do not.** `models.py` is a pure
type-stub tier (at runtime a lazy value is an `Expr` recorder — zero runtime);
`surface.py` is the *one* eager runtime (a `__getattr__` that dispatches to a live
core); `surfaces.py` is the concrete eager classes + the client machinery. The op
**signatures** already live in one place (the backings) and are generated into all
three tiers, `--check`-gated — so there is no hand-maintained duplication to
remove there.

**But the user's intuition is partly right for the *eager data surfaces*.**
`Document`/`Reference` still carry hand-written `__init__` + serialization bodies
that can be hoisted into the `Surface` base, collapsing each to essentially
`@surface(Core) class Document(Surface[DocumentCore]): <generated stub block>` — a
"simple model + generated types", exactly the shape of `LazyDocument`. The
**client tier** (`WebClient`/`AsyncWebClient`/`Session`) cannot and should not
collapse: it is real lifecycle/session/pool logic, not mechanical wrapping.

## What each layer actually is (runtime vs type-check)

| Layer | Runtime role | Type-check role | Hand-written vs generated |
|---|---|---|---|
| `surface.py` — `Surface(Generic[C])`, `wrap`, `@surface` | **The entire eager runtime.** `Surface.__getattr__` reads a core field, dispatches a prop/call op to the core's backings, and `wrap()`s any Core result back into its surface (cached on `core._surface` for identity). `wrap` also builds `Collection`s. | `Surface[C]` gives subclasses a precisely-typed `_core`. | Hand-written; ~110 lines; essential and irreducible. |
| `surfaces.py` — `Reference`, `Document`, `_ClientBase`/`WebClient`/`AsyncWebClient`, `Session`, `Renderer`, `from_url`, `default_client`, `RemoteWebClient` | The concrete classes `wrap` instantiates (`Reference`/`Document`) + the client surfaces users hold (lazy recorders) + lifecycle. | Each carries a generated `>>> generated <<<` TYPE_CHECKING block of op signatures. | **Mixed**: signatures generated; `__init__`, serialization proxies, context managers, session/pool/bus accessors, the client `__getattr__` recorder are hand-written. |
| `models.py` — `Lazy`, `LazyField`, `LazyReference`, `LazyDocument`, `LazyCollection` | **None.** At runtime every lazy value is an `Expr` (`query/expr.py`); these classes are never instantiated. | The lazy authoring types (`wq.doc: LazyDocument`, `.collect() -> Document`). | **Fully generated** whole classes (`_lazy_class` emits head + body), plus two hand-written leaves (`LazyField`/`LazyCollection`). |

The asymmetry the user noticed is real and has a cause: `gen_stubs._lazy_class`
emits **entire** classes (the lazy tier is type-only, so nothing else is needed),
while the eager tiers emit only the **member block** that drops inside a
hand-written class (because that class must exist and behave at runtime).

## What genuinely cannot collapse

1. **Eager needs a runtime object; lazy does not.** An eager `Document` holds a
   live `DocumentCore` and dispatches ops to it *now*; a `LazyDocument` is a
   fiction over an `Expr` that records. You cannot make the eager tier "just type
   stubs" — something must dispatch at runtime. That something is `Surface`. So
   `surface.py` stays.
2. **Named concrete types are required.** mypy/pyright need distinct
   `Document`/`Reference`/`LazyDocument`/… classes to hang the generated member
   sets on. You cannot replace them with one generic `Surface` and keep precise
   types. So the concrete classes (even if reduced to a decorator + stub block)
   stay.
3. **The signature "triplication" is single-sourced.** The same op appears as an
   eager stub (surfaces.py), a lazy stub (models.py) and a collection lift
   (collection.py) — but all three are *generated* from the backing signatures by
   the one `members()`/`lift_members()` walk and gated by `gen_stubs --check`.
   This is generated projection, not hand-maintained duplication; leave it.
4. **The client tier is bespoke.** `WebClient`/`AsyncWebClient`/`Session` are not
   mechanical core-wrappers: context managers, `close/aclose`, `session()`
   (core-swap → local `Session` or remote handle), `bus`/`pool`/`scope`/`use`,
   `document`/`reference` recovery, and the lazy `__getattr__` that records
   `WebClient`-rooted plans. This is real behaviour; generating it would buy
   nothing and cost clarity.

## The genuine win: collapse the eager *data* surfaces

Today `Reference`/`Document` each hand-write:
- an `__init__` that accepts a core **or** builds one from `**fields`;
- (Reference) four serialization proxies (`model_dump`/`model_dump_json`/
  `model_validate`/`model_validate_json`).

Both are mechanical and can move into `Surface`, because a surface already knows
its core class (via the `@surface(Core)` registry — store the inverse on the class
as `_core_cls`). Then:

```python
# surface.py (sketch)
class Surface(Generic[C]):
    _core_cls: ClassVar[type]                      # set by @surface
    def __init__(self, core=None, **fields):
        if not isinstance(core, self._core_cls):
            known = {k: v for k, v in fields.items() if k in self._core_cls.model_fields}
            core = self._core_cls(**known)
        object.__setattr__(self, "_core", core)
    def model_dump(self, **kw):        return self._core.model_dump(**kw)
    def model_dump_json(self, **kw):   return self._core.model_dump_json(**kw)
    @classmethod
    def model_validate(cls, data, **kw):      return cls(cls._core_cls.model_validate(data, **kw))
    @classmethod
    def model_validate_json(cls, data, **kw): return cls(cls._core_cls.model_validate_json(data, **kw))
```

`@surface(core_cls)` sets `cls._core_cls = core_cls`. Now the concrete eager data
surfaces become exactly what the user pictured:

```python
@surface(DocumentCore)
class Document(Surface[DocumentCore]):
    if TYPE_CHECKING:
        # >>> generated: Document eager surface <<<
        ...
```

— a decorator + a generated stub block, no hand-written body. That is the same
shape as `LazyDocument`, one tier down. `wrap()` is unchanged (it already does
`cls(value)`), and the serialization passthrough is now inherited (harmless on
Document, which simply never dumps).

### Options weighed

| Option | Buys | Costs / breaks |
|---|---|---|
| **(a) Hoist ctor + serialization into `Surface`** *(recommend)* | `Document`/`Reference` bodies → ~0; less to hand-maintain; matches the lazy-tier shape | Small `Surface` change; must set `_core_cls` via `@surface`; verify `model_dump` inheritance doesn't widen the typed surface (keep it out of the generated stub, or type it on the classes that should expose it) |
| (b) Fully generate the eager concrete classes (bodies too) | One more thing generated | The generator must emit runtime code, not just stubs — a big step up in generator complexity and risk, for classes that are already tiny after (a). Not worth it. |
| (c) Merge the three modules into one | Fewer files | Tangles type-only and runtime concerns; risks import cycles (`surfaces`→`surface`, `collection`↔`surfaces`); the lazy tier wants to stay a clean type-only island. No real win. |
| (d) Leave as-is | Zero risk | Keeps ~2 small hand-written bodies that (a) removes cleanly. |

Merging `surface.py` into `surfaces.py` is possible (surface.py is small) but the
split usefully keeps the *base runtime* importable without the concrete classes
(avoids cycles with `collection`/`wrap`); keep them separate.

## Recommendation

1. **Keep the three-layer split** — it encodes the essential eager-runtime vs
   lazy-type-only axis, and the signature projection is already single-sourced.
2. **Do option (a):** hoist the generic constructor + serialization passthrough
   into `Surface` so `Document`/`Reference` collapse to `@surface(Core)` + a
   generated stub block — the "simple model" the user intuits, for the data
   surfaces.
3. **Leave the client tier bespoke** (`WebClient`/`AsyncWebClient`/`Session`):
   real lifecycle logic, correctly hand-written.

### Migration sketch (strangler, each step green-gate-able)

1. Add `_core_cls: ClassVar[type]` to `Surface`; have `@surface(core_cls)` set it.
   No behaviour change. Gate.
2. Add the generic `__init__` + serialization methods to `Surface`. Gate (nothing
   uses them yet; existing overrides still win).
3. Delete `Document.__init__`; rely on the base. Gate (`wrap`, construction,
   `demo`, fixture).
4. Delete `Reference.__init__` + its four serialization proxies; rely on the base.
   Gate (Reference is dumped to the wire in `service`/`remote`/tests — exercise
   those).
5. Confirm `gen_stubs --check` still clean and the generated blocks are the *only*
   members on `Document`/`Reference`. Update the module docstring to say the data
   surfaces are decorator-plus-stub.

### Risks

- **Serialization on the base widens intent:** every `Surface` gains `model_dump`.
  Harmless at runtime (delegates to the core), but keep these methods *out* of the
  generated typed stub for surfaces that shouldn't advertise them, or accept them
  as universally available (a `Document` core can `model_dump` too). Decide
  deliberately.
- **`_core_cls` vs the `_REGISTRY`:** two sources for the core↔surface link; set
  both from the one `@surface` decorator so they can't drift.
- **`Session` is not a `Surface`** (it wraps a session core by hand) — out of scope
  for (a); leave it, or make it a `Surface[WebSessionCore]` in a later, separate
  step if its accessors line up with generated props.

Net: the layering is sound; the reachable simplification is small and local —
shrink the two eager *data* classes to generated stubs by moving their mechanical
bodies into the shared `Surface` runtime. The client tier and the three-tier
split stay.
