# Review checklist — architecture & code smells

A practical checklist for reviewing changes to `packages/` (the `web.*` clean-room stack). It is
grounded in what an extended hardening pass actually found — not textbook generalities. Run through
it for any non-trivial change; skip what plainly doesn't apply. Companion to `ARCHITECTURE.md`
(which describes the layers) — this is *how to keep them honest*.

How the last full pass went: the **architecture stayed clean** (acyclic layer DAG, no forks, no
stray coupling) and **11 real latent bugs** were found by systematically stress-testing real-world
input messiness — every one passed typechecks and happy-path tests but broke on live content. Those
bug classes are section 5 below.

---

## 1. Architecture invariants (the arrangement is load-bearing)
- [ ] **Acyclic layer DAG** — each package imports only *lower* layers. Order:
      `fetch → parse → resolve → crawl → dsl` and `parse → … → onboard` (the agent tier lives inside onboard).
      Verify with **runtime imports** (`grep -rn "^from web\." packages/<L>/web`), not a graph
      tool's name-matching (those give false positives, e.g. a `.doc` attr ≠ the DSL's `.doc()`).
- [ ] **`fetch` is backends only** — no ladder/tiers/escalation there; that is `resolve` policy.
- [ ] **DSL is a lazy engine on top** — base layers are plain async code; recording/dispatch is DSL.
- [ ] **Sessions own their state** — a live page belongs to a `BrowserSession`; nothing else
      opens/closes it. `HttpFetcher.fetch` is stateless (jar cleared); the session persists it.
- [ ] **Signals (evidence) → Flags (conclusions + remedy)** — detection stays in that shape.
- [ ] **The LLM is the `onboard` tier**, not a separate low layer; `agent` is LLM-agnostic (a
      `Driver`/`Policy` is any callable).
- [ ] **No custom base classes.** Use structural `Protocol`s (`Fetcher`, `Session`, `Llm`,
      `_Openable`) and generics (`BoundedLoop[S,O,D]`). "Same pattern everywhere, no custom bases."
- [ ] **New cross-cutting capability** (tools, a service, MCP) — decide its *tier* deliberately;
      putting it in the wrong package means moving it later (churn). Prefer merging into an existing
      tier over inventing one.

## 2. Typing (no `Any` in signatures)
- [ ] Audit with **`grep -rnw Any`**, not `: Any` — the narrow form misses `dict[str, Any]`.
- [ ] JSON → **`pydantic.JsonValue`** (recursive, model-safe; a hand-rolled recursive `Union` as a
      model field causes a pydantic RecursionError). DOM nodes → **`Node`** (`lxml _Element`, via
      `lxml-stubs`). Genuinely dynamic dispatch (a reflective `getattr` executor) → **`object` +
      `isinstance` narrowing**, not `Any`. Optional-dep types (playwright) → **`TYPE_CHECKING`
      import** so the annotation exists without the runtime dep.
- [ ] JSON-serialisable `**kwargs` passthrough to a stdlib fn is the one defensible `Any` — comment it.
- [ ] Run **`mypy --strict` and `pyright` PER PACKAGE** (`packages/<L>/web`). Never multi-package —
      the `web` namespace collides ("source file found twice"). Pyright catches nullability mypy
      misses (narrow instance attrs into locals before returning).

## 3. DRY — no forked logic
- [ ] No helper copied across modules. One home: `parse/nodes.py` (`tag`/`text`/`query`/`Node`),
      `parse/classes.py` (class-token filter), `parse.jsonpath.dig` (reused by `resolve`).
- [ ] Same-named funcs in two files are only OK when genuinely different domains (`skeleton()` =
      JSON vs DOM; `mw()` = a per-factory middleware closure) — confirm, don't assume.
- [ ] **"Don't reinvent" ≠ "always use stdlib."** Verify the stdlib's *semantics* first:
      `urllib.robotparser` uses outdated first-match precedence + no wildcards, so we kept a modern
      longest-match matcher. Reuse stdlib where it's actually correct (`codecs`, `gzip`, `dateutil`,
      `contextvars`, `JsonValue`).

## 4. Imports
- [ ] **Module-level only.** No function-local imports. The sole justified exception is an
      **optional-dependency lazy import** (playwright in `browser._browser_ready`) — a delegating
      facade does *not* need inline imports (siblings importing back only under `TYPE_CHECKING`
      don't cycle).
- [ ] A new dependency has a matching `[tool.uv.sources]` path entry (a shared env can hide a
      missing one until a fresh install).

## 5. Robustness on real-world content (where the bugs live)
- [ ] **Encodings** — validate a declared charset against `codecs.lookup` (unknown → utf-8 fallback,
      else `.decode()` raises `LookupError` that `errors="replace"` can't catch). When sniffing
      text by decoding a byte prefix, use an **incremental decoder** so a multibyte char split at the
      cut isn't mis-sniffed as binary.
- [ ] **Compression** — gzipped payloads *not* signalled by HTTP `Content-Encoding` (e.g.
      `sitemap.xml.gz` served as `application/gzip`) arrive as raw bytes; detect the magic
      (`1f 8b`) and decompress.
- [ ] **Whitespace-significant content** — parse JSON/JSON-LD from **raw** node text, never the
      whitespace-collapsed text (collapse corrupts string values like `"ACME  Corp"`).
- [ ] **Aggregation / merge** — never concatenate whole `<html>` documents (lxml keeps only the
      first root → later pages silently lost). Merge `<body>` *inner* content under one root.
- [ ] **Retry / transient** — match the *actual* error codes the classifier emits
      (`fetch.timeout|connect|dns|proxy`), not just a generic fallback; keep persistent errors
      (`tls|url|redirects`) non-retriable.
- [ ] **Collections** — dedup markers must be hashable; route an unhashable key value (list/dict)
      through a hashable form.
- [ ] **Empty / degenerate input** — guard unpacks (`header, *body = matrix` on an empty matrix);
      a missed `select` returns `None` not a crash; reads are *symmetric* (markup reads no-op on a
      JSON doc ⇒ JSON reads must no-op on a non-JSON doc, not raise).
- [ ] **Loops** — to disable stall detection pass **no** `progress` fn; a constant-returning one
      (`lambda s: None`) makes `mark == prev` every round and *falsely* stalls after `max_stalls`.
- [ ] **Spec matching** — honour `*`/`$` wildcards and match against path **+ query** (robots,
      RFC 9309).
- [ ] **Backends never leak** — raw `httpx`/`playwright` exceptions become a classified
      `snapshot.error`; `CancelledError` (a `BaseException`) still propagates.

## 6. Interface hygiene / compaction
- [ ] Prefer a **Facade** (thin front doors delegating to sibling modules, e.g. `Document`) over a
      god object; keep the surface a consumer/model reasons over small.
- [ ] Recorded plans stay **serialisable** (`Step.args: list[JsonValue]`) so new features survive
      remote dispatch — test a blob round-trip when adding a plan field (`documents()` `follow` /
      `doc_reads`).
- [ ] Additive, backward-compatible extensions over new surfaces (`project(url="a@href")` extended
      the existing method; `documents()` reused `Document`, no new class/fork).

## 7. Tests & gate
- [ ] Every bug fix ships a **regression test** that fails before and passes after.
- [ ] Tests are offline/deterministic (`pytest_httpserver`, canned bytes; inner `async def` +
      `asyncio.run`, no `pytest.mark.asyncio`).
- [ ] Full gate green before commit: **`mypy --strict` + `pyright` (per package) + `pytest`**, and
      `packages/demo.py` still runs (it exercises the whole stack end-to-end).
- [ ] Never leave the tree broken; commit small green steps.
