# `webclient` package — deep code review

Scope: the `webclient` package, with the focus the brief asked for — query building
and the query DSL/executor first, then the onboarding pipeline, signals/flags, browser
transport, and core dispatch. Read-only analysis; nothing here was committed and no
network onboarding run was performed. Claims marked **[verified]** were reproduced with
a scratch script against the repo's own `env/` (Python 3.12).

---

## 1. Architecture overview

The package is a clean, layered, declarative web client:

- **`core/web_core.py` — `WebCore` + `Backing`.** Every core (client / document /
  reference / crawl / session) is a `WebCore` that *chooses* the backings that apply to
  its current state and *dispatches* an op to the first backing that `provides` it.
  `Document` composes ten backings (status/events/live/html/json/transport/metadata/
  structure/flags/regex). This is a genuinely nice abstraction: adding a medium op is
  additive, and sync/async/remote are just dispatch modes (`_dispatch_mode`), so `dispatch`
  and `__getattr__` share one mode-aware path.
- **`query/` — the lazy DSL.** `Expr` is a recorder: every attribute/call/operator returns
  a new `Expr` extending an immutable `Plan` (a pydantic model, hence the wire form). The
  `executor` walks the plan once, async, driving the eager surface by `getattr`/`await`.
  `Collection`/`Field` are the two surfaces that carry behaviour (row shaping, scalar leaf).
- **`clients/` — transport.** `ClientPool` leases http clients (recycled) and browser
  pages (closed on release), each kind bounded by a semaphore. `BrowserFactory`/
  `BrowserClient` own Playwright.
- **`signals/` — detection.** Tiered detectors → noisy-OR confidence → `Flag`s. Isolated
  from the cores; a remote resolve re-derives the same flags.
- **`pipelines/onboarding.py` — the LLM pipeline.** Seven stages (search → crawl → select →
  evaluate → reference → resolve → query) plus opt-in review gates.

**Do the abstractions hold up?** Mostly yes. The backing/dispatch model and the
Plan-as-wire-form are the strongest parts. The two places where the abstraction leaks are
(a) the **executor's per-element browser-page release** (`_PLAN_LIVE` ContextVar plumbing,
`executor.py:41-185`) — correct but intricate, and the single most likely place for a
future deadlock regression; and (b) the **"self-contained executable query"** claim in the
pipeline, which is not actually self-contained (see F2). The single-engine-loop design
means the concurrent-harness path is far safer than it looks (§5).

---

## 2. Prioritised findings

### F1 — **Remote code execution in `_parse_query` via `eval` [verified] — CRITICAL**

`webclient/pipelines/onboarding.py:1227`

```python
expr = eval(code, {"__builtins__": {}, "wq": wq})  # noqa: S307 - our DSL, restricted ns
```

The query string comes from the LLM, which authors it from an attacker-influenceable page
skeleton (classic prompt-injection surface). `{"__builtins__": {}}` is **not** a sandbox.
`_query_code` only requires the string to *start* with `wq.`, and `wq` is a live
`WebQuery` object whose bound methods (`reference`, `filter`, `when`, …) are ordinary
functions carrying `__globals__` — and every module `__globals__` contains the real
`__builtins__`. The `isinstance(expr, Expr)` rejection happens **after** `eval` returns, so
any side effect has already run.

**[verified]** reproduction (marker file was actually written during `eval`):

```python
payload = ("wq.reference.__globals__['__builtins__']['__import__']"
           "('os').system('echo PWNED > /tmp/pwned')")
_parse_query(payload)   # raises "query is a int, not a wq.doc chain" — AFTER os.system ran
# -> /tmp/pwned exists, contents: PWNED
```

**Failing scenario.** A crawled page contains text engineered so the model emits
`wq.reference.__globals__[...]...` as its "query". Running onboarding against a hostile
site can execute arbitrary code on the operator's machine / CI.

**Fix.** Do not `eval` model output. The DSL already has a safe authoring path: ask the
model for the `to_blob()` JSON (or the `describe()` form) and load it through
`from_blob`/`from_explain`, both of which call `Plan.validate_names()` (the real safety
boundary). If a `wq.doc` code form is desired for ergonomics, parse it with `ast` and walk
the tree yourself, permitting only `Name('wq'|'doc'|'field'|…)`, attribute access on
non-dunder names, calls, and literal/operator nodes — reusing the `_node_plan` machinery
already in `expr.py`. At minimum, reject any `code` containing `__` before eval (necessary,
not sufficient).

---

### F2 — **The "self-contained executable query" silently drops proxy/antibot — HIGH**

`webclient/pipelines/onboarding.py:1173-1186` (`_executable_query`)

```python
tier = resolve.browser.when if (resolve and resolve.browser) else None
rooted = wq.reference(url).resolve(browser=tier) if tier else wq.reference(url).resolve()
```

Only the **browser tier** is baked into the blob. The `Resolve` policy's `proxy` and
`antibot` (the remedies `write_resolve` selects for an `anti_bot_triggered` source,
`onboarding.py:1099-1116`) are **not** encoded — the code comment even admits it
("proxy/antibot are transport concerns a lazy `.resolve()` can't encode"). But the artifact
and the summary advertise the blob as runnable as-is:
`"query blob (copy; run with from_blob(blob).collect())"` (`onboarding.py:523`), and
`run_query` (`:1372`) re-runs exactly this blob.

**Failing scenario.** A source that onboarded *only because* it was fetched through a proxy
+ stealth browser (anti-bot triggered) produces a blob that, re-run later via
`from_blob(blob).collect()` or `run_query`, fetches **without** proxy/antibot → gets
blocked/challenged → 0 rows. The pipeline reported it as onboarded and gives the user a
blob that doesn't reproduce. This is the pipeline's own "the reference didn't match the
source" class of bug, reintroduced one layer down.

**Fix.** Either (a) encode proxy/antibot into the executable form (extend the lazy
`resolve()` op to carry the full `Resolve`, or attach the `Resolve` to `QueryArtifact` and
have `run_query`/the docs apply it), or (b) stop calling the blob "self-contained /
executable as-is" and store the `Resolve` alongside it as the required companion.

---

### F3 — **A query missing a required field is still reported `ok=True` [verified] — HIGH**

`webclient/pipelines/onboarding.py:1445-1468` and `:1838-1845`

Inside the retry loop the completeness check (`missing = _empty_required_fields(good, brief)`)
correctly blocks *early acceptance* — a query is only returned immediately when
`tested and good and not missing`. But the fallback keeps the **first** artifact regardless
of quality:

```python
best = best or art               # :1459  (art.row_count = len(good), may have missing req fields)
...
return best                      # :1468  when every attempt failed the full bar
```

and the orchestrator's success test only looks at row count:

```python
result.ok = q is not None and q.row_count > 0    # :1839
```

**[verified]** — with an LLM that returns, on *every* retry, a query that fills `title` but
leaves the required `date` empty (wrong optional selector), `write_query` returns
`row_count=1`, `sample=[{'title':'A','date':None}]`, and `_onboard_company` sets `ok=True`.
`test_write_query_rejects_a_missing_required_field` only covers the case where a *later*
retry fixes it; the all-retries-exhausted path is untested.

**Fix.** Track whether the returned artifact actually met the completeness bar (e.g. only
set `best` for artifacts with `not missing`, or add `QueryArtifact.complete: bool` and gate
`result.ok` on it). At minimum, when returning an incomplete fallback, record the missing
fields in `result.reason` so success isn't reported silently.

---

### F4 — **`tested=True` does not test the stored blob — MEDIUM**

`webclient/pipelines/onboarding.py:1440,1447-1451`

`_test_query` collects the **document-level** `expr` (`wq.doc…`) against the already-fetched
`doc`. The stored artifact is a **different** expression — `_executable_query` re-roots it at
`reference(url).resolve(browser=tier)`. So `tested`/`row_count`/`sample` describe the
extraction run against the write-time `doc`, never the blob that ships.

Two ways this diverges in practice: (1) `doc = wc.fetch(candidate_url, browser=browser…)`
uses `browser="auto"` (`:1408`) while the blob may force `browser="always"` (SPA) — the
tested DOM and the executed DOM differ; (2) F2 — the blob omits proxy/antibot, so a re-fetch
can fail entirely. `tested=True` is therefore an over-claim.

**Fix.** Test the executable artifact end-to-end at least once (collect the re-rooted expr),
or rename the field to reflect that it's the extraction-against-write-time-doc that passed.

---

### F5 — **Default "stealth" browser leaks `HeadlessChrome` in the UA [verified] — MEDIUM (evasion realism)**

`webclient/clients/browser.py:329-421`, `webclient/settings.py:68` (`BrowserConfig`:
`headless=True, stealth=True, fingerprint=False, channel=None`).

`_STEALTH_JS` masks `navigator.webdriver`, plugins, WebGL vendor, etc., but **not** the
user-agent. The UA is only overridden when `fingerprint=True`, which is **off by default**.
So the default, stealth-on configuration ships the most trivial bot tell of all.

**[verified]** default bundled headless chromium:

```
UA: Mozilla/5.0 (X11; Linux x86_64) … HeadlessChrome/151.0.7922.34 Safari/537.36
navigator.webdriver: False
```

`HeadlessChrome` appears in both `navigator.userAgent` and the HTTP `User-Agent` header. Any
real anti-bot service keys on this immediately, defeating the rest of the stealth work for
the default config.

Secondary realism gaps in the same area:
- The `_FINGERPRINTS` UAs claim `Chrome/139–140` while the bundled engine is `151` — a
  version-mismatch tell even *with* `fingerprint=True`.
- Fingerprints spoof Windows/macOS UAs while running on Linux, and `_STEALTH_JS` hardcodes
  `Intel Inc.`/`Intel Iris` WebGL regardless of the chosen identity → inconsistent
  fingerprint (OS in UA vs. platform vs. GPU).
- `Object.defineProperty(o, p, {get: () => v})` (browser.py:332) omits `configurable` and
  leaves the getter's `.toString()` as `() => v` — itself a detection vector; real stealth
  patches `Function.prototype.toString`.

**Fix.** Default `fingerprint=True` (or always set a realistic non-headless UA when stealth
is on); derive the spoofed Chrome version from the running engine; make GPU/platform
consistent with the chosen UA; consider `channel="chrome"` by default where available.

---

### F6 — **`open()` accumulates `console`/`request` listeners on page reuse — MEDIUM**

`webclient/clients/browser.py:236-238`

```python
page.on("console", lambda m: console.append(...))
page.on("request", lambda r: network.append(...))
```

These are registered every `open()` call and never removed. Pages are normally leased once,
but a `BrowserClient` reused for a second `open()` (e.g. an internal re-navigation, replay,
or any future page-recycling) double-counts every console line and network request into the
new capture — inflating `_injection`'s XHR counts and thus the SPA flag. Pages are in the
non-recycled pool today, so impact is latent, but it's a correctness landmine.

**Fix.** Capture listeners with `page.remove_listener` in a `finally`, or register once in
`__init__` and clear the buffers per `open()`.

---

### F7 — **Timeliness gate: nested date fields and loose name matching — MEDIUM**

`webclient/pipelines/onboarding.py:1586-1623` (`_timeliness`)

- `date_cols = [f.split(".")[0] …]` then reads `r.get(c)` and requires `isinstance(..., str)`.
  For a **nested** date (`meta.published`), the top-level column `meta` holds a dict, not a
  string → no dates parsed → `("", False)` → timeliness silently never assessed. Any brief
  whose date lives under a branch escapes the one deterministic gate the pipeline currently
  enforces.
- The name heuristic matches any field containing `date/publish/time/year` as a substring —
  `runtime`, `timezone`, `yearly_revenue`, `datetime_updated` all qualify. `_parse_date`
  then fails on their values and they drop out, but a field like `year` holding `"2019"`
  won't parse under any of the `strptime` formats (no bare-year format), so a genuinely
  year-only dataset is also un-assessed.

**Fix.** Resolve nested date columns against the row shape (walk dotted paths into nested
dicts), and add a bare-`%Y` parse. Tighten the name match to word boundaries.

---

### F8 — **`evaluate_candidates` prefers *any* queryable source over a much cleaner one — LOW/MEDIUM (design)**

`webclient/pipelines/onboarding.py:1073-1077`

```python
rank = (ev.is_queryable, ev.scrapability)          # bool first
if best is None or rank > (best.is_queryable, best.scrapability):
```

Because `is_queryable` (a bool) is the primary key, a queryable source with
`scrapability=5` beats a non-queryable one with `scrapability=10`, and the early-return only
fires for `usable and is_queryable`. A pristine static table (must-tier, scrapability 9, not
"queryable") is only chosen if no queryable page exists at all. That's defensible as a
stated preference, but it's a sharp cliff driven entirely by a boolean the LLM sets — worth
a weighted score (e.g. `scrapability + queryable_bonus`) instead of lexicographic bool-first.

---

### F9 — **Seed verification can fail *closed* and sink the company — LOW**

`onboarding.py:696-716` / `search_web:731-742`

`_seeds_for_company` fails **open** only when the model gives no usable judgement (non-list
`belong`). If the model returns an explicit `belong: []` (a false-negative — it wrongly
believes none belong), all seeds are dropped, `search_web` retries once, and a second empty
verdict returns `[]` → the run ends with `reason="no search seeds"`. A single over-cautious
LLM reply kills the company. Consider treating "results came back but the model kept none"
as a low-confidence signal that also falls back to the filtered seeds after the retry, or
require a minimum-drop-ratio before trusting a total rejection.

---

### F10 — **Relative XPath field selectors leak to the whole document — LOW (query robustness)**

`webclient/core/document/html.py:666-674` (`_find`) uses `root.xpath(selector)` where `root`
is the selected element. lxml evaluates a leading-`//` XPath from the **document root**, not
the context node. CSS `select` inside `extract` is correctly scoped (`element.cssselect`
searches descendants — **[verified]** with `:has`, `:scope + p`, and `//p[a[contains…]]`
row selectors, all of which work), but if the model writes an XPath **field** selector like
`//span[@class="price"]` inside a per-row `extract`, it matches across the entire page, not
within the row. The skill only demonstrates XPath for the *row* selector, so this is a
latent authoring foot-gun rather than a live bug. Consider rewriting a leading `//` to `.//`
for element-scoped `_find`, or documenting the restriction.

---

## 3. Query-building failure modes (the highest-real-failure-rate area)

Findings F1–F4, F7, F10 are all in this path. Summarising the *why queries fail* analysis
the brief asked for:

1. **Authoring is `eval`, not parsing (F1).** Beyond the RCE, this means any Python the
   model emits that isn't a pure `wq` chain either raises (→ retry) or, worse, runs. The
   safe `from_blob`/`from_explain` round-trip already exists and is under-used — the model
   is asked to write code precisely because it "gets `to_blob` wrong" (`_parse_query`
   docstring), but the remedy is a proper parser, not `eval`.
2. **Test ≠ ship (F2, F4).** The tested expression and the stored/re-run blob are different
   objects fetched under different policies, so a "tested, N rows" artifact can fail on
   re-run. This is the biggest gap between "the pipeline says it works" and "it works."
3. **Completeness gates acceptance but not success (F3).** Partial extractions leak through
   as `ok=True`.
4. **Selector robustness is actually good.** `_no_rows_hint`/`_selector_match_count`
   (`:1247-1283`) distinguish "row selector matched nothing" from "rows matched but fields
   empty" and feed that back — a genuinely strong retry loop. `_populated_rows` correctly
   rejects all-empty rows so an over-broad row selector can't fake success. The `skeleton`
   with `[xhr]`/`[js]` origin annotation (`html.py:310-395`) is a real asset for steering
   the model away from client-rendered content a static query can't reach.
5. **`_data_rows` correctly counts an un-projected `select_all` as zero rows**
   (`:1142-1158`) — good, this is the classic "looks like success" trap, closed.

Reproducible constructions used above: the RCE (F1) and the missing-required-field
success (F3) both reproduce from a few lines against `env/`. The `:scope + p` / `:has()` /
XPath-row patterns from `skills/lazy-queries.md` all execute correctly against the real
engine (I verified them), so the *documented* patterns are sound — the failures are in the
pipeline's authoring/validation/packaging around them, not the DSL.

---

## 4. Signals / flags detection

Generally solid and well-isolated. Notes:

- **`_combine` noisy-OR (`registry.py:79-84`)** is fine, but there is **no negative
  evidence** — flags only ever accumulate confidence. A page with a `<form>` and a
  `role=button` will always trip `forms`/`buttons` even when they're irrelevant chrome; the
  pipeline treats `forms|buttons` as `interactive` (`onboarding.py:1020`) which can push a
  perfectly static listing toward "needs a browser session." Low impact but worth knowing
  the detectors are one-directional.
- **`_injection` XHR host parse (`dom.py:68-82`)** swallows every exception from
  `req.dispatch("url")` silently (`except Exception: continue`) — a malformed request event
  just vanishes from the same-origin/cross-origin counts, which can flip the SPA verdict with
  no trace. Consider debug-logging the drop.
- **`attach_shadow_marker` (`dom.py:150-155`)** matches the substring `attachShadow` /
  `shadowrootmode` anywhere in page text, including inside an article *about* web components
  or a JSON blob — a static false-positive for `shadow_dom` (→ forces a browser render). It's
  only 0.5 confidence (below the 0.5 present threshold on its own — actually `>=` so it is
  exactly present), so a single mention flips the flag. Tighten to script/template contexts.
- The double-counting risk from F6 feeds directly into these detectors, so F6 is partly a
  signals-correctness bug too.

---

## 5. Concurrency / thread-safety (the concurrent-harness path)

The harness (`scripts/onboard_harness.py:413-416`) drives **one shared `WebClient`** from a
`ThreadPoolExecutor`. This is far safer than it appears because of the architecture:

- **All engine I/O funnels through one `EngineLoop`** (one daemon thread, `loop.py`). Sync
  facade calls bridge via `run_coroutine_threadsafe` (`bridge:288-308`, `loop.run:51-63`),
  which is thread-safe. So the pool counters, `_host_next`, `_idle` lists, etc. are only
  mutated **on the single loop thread** — no true data races.
- **`_PLAN_LIVE` is a ContextVar** and each cross-thread `submit` starts a fresh task with a
  fresh context, so plan-owned page release is isolated per run (`executor.py:143-185`). Good.
- `default_client()` is lock-guarded (`__init__.py:945`).

Real issues on this path are **logical, not races**:

1. **No per-company isolation.** The harness comment says "a session's fetches lease pages"
   but `_run_case` calls `onboard_company(wc=wc, …)` directly — **no `wc.session()`**. All
   concurrent companies share one client's cookies/`default_headers`/`_page_scripts`/
   `_backings`. Cross-company cookie bleed and shared identity are possible. If isolation is
   intended, each case should run on its own `wc.session()`.
2. **`min_interval` pacing is shared and best-effort** (`_pace:392-412`) — N threads hitting
   the same host still bunch (the code says so). Fine, but the "politeness" guarantee is
   weaker than it reads under parallelism.
3. **Pool page accounting** (`pool.py:119-137`): `_free("page") = limit - held`. Safe
   (semaphore bounds `held ≤ limit`), but `_created["page"]` is a cumulative counter never
   used for pages and `stats.pages_total` returns the *limit*, so "total/free" for pages is a
   capacity view, not a live population — matches the "known pool-accounting lead" in memory.
   Not a bug, but the numbers mislead.

---

## 6. Core dispatch / executor edge cases

- **`fan_out_stream` cancellation (`executor.py:528-572`)** is careful — drains queued
  siblings, cancels outstanding tasks in `finally`, awaits them. Good. `fan_out`
  (`:504-525`) surfaces the first failure and notes siblings via PEP 678. Solid.
- **`_leases_pages`/`_fanout_limit` (`:84-112`)** only counts `resolve`/`fetch` with an
  explicit `browser in (True,"always")` as page-leasing; an `"auto"` branch that *escalates*
  to a browser is scheduled at http width (10) but bounded by the page semaphore. The comment
  argues the per-element release drains it, but under a fan-out of many auto-escalating
  elements this over-subscribes the page pool and inflates `waiting` — the exact scenario the
  memory's "pool-accounting lead" hints at. Worth a targeted test.
- **`_start` for a Reference root (`:300-327`)** binds `core._client = client`; if `client`
  is `None` (a plan collected with no bound client and no default reachable) a later IO op
  falls back to `default_client()` inside `_bridge_io` — generally fine, but a plan rooted at
  a bare `Document`/`Field` with `context=None` raises a clear error. OK.
- **`Field.is_empty` (`collection.py:50-51`)** treats `0`/`0.0`/`False` as *present* (only
  `"" [] {} None` are empty) but `Field.__bool__` returns `bool(self._value)`, so a numeric
  `0` field is `ok`/non-empty yet falsy — a `filter(... .is_ok())` and a truthiness filter
  disagree on `0`. Niche, but a dataset with legitimate `0` values (price, count) can behave
  surprisingly under a truthiness filter. Document the distinction.

---

## 7. Test-coverage gaps

The suite is broad (37 files; onboarding alone has 52 tests) and the query retry/validation
loop is well covered. Missing:

- **The F3 fallback path** — all retries fail the completeness bar but a partial artifact is
  returned and `ok=True`. (`test_write_query_rejects_a_missing_required_field` only tests
  recovery on a later retry.) Add a "every retry leaves a required field empty → not ok" test.
- **F1** — no test asserts `_parse_query` refuses a non-`wq` / dunder-bearing string *before*
  eval side effects. A security regression test (`_parse_query("wq.reference.__globals__…")`
  must not execute) is warranted.
- **F2/F4** — no test runs the stored `blob` end-to-end for a proxy/antibot or SPA source and
  asserts it reproduces the tested rows.
- **F6** — no test opens the same `BrowserClient` twice and asserts events aren't
  double-counted.
- **F7** — no test for a nested date column (`meta.published`) reaching the timeliness gate.
- **Concurrency** — `test_session`/`concurren*` exist, but nothing asserts cookie/identity
  isolation (or the deliberate lack of it) when N threads share one `wc` in the pipeline.

**Where the gate (`gen_stubs --check`, `mypy --strict`, `pyright`, `pytest`) can miss real
bugs.** The gate is type/stub-focused; every finding above is a **runtime-semantics** bug
that types can't catch:
- F1/F3/F7 involve `Any`-typed LLM JSON and string heuristics — `mypy --strict` sees `Any`.
- F2/F4 are about *which object* is tested vs shipped — both are well-typed `Expr`s.
- F5/F6 are Playwright behaviour (`page` is `Any` by the project's own override,
  `pyproject.toml:89-92`), invisible to the checkers.
- Much of the pipeline uses broad `except Exception` / `# noqa: BLE001` (e.g.
  `_test_query:1367`, `_selector_match_count:1256`, `run_query:1382`) which is deliberate but
  means silent-failure spots the gate never flags — F9's fail-closed and the F2 re-run
  failure both hide behind such swallows.

---

## 8. Quick wins vs larger refactors

**Quick wins**
- **F1**: swap `eval` for the existing `from_blob`/`from_explain` (or an `ast` allowlist);
  add a dunder/`__` reject. Highest value-per-line in the package.
- **F3**: only set `best` for artifacts with `not missing`, or gate `result.ok` on a new
  `QueryArtifact.complete`.
- **F6**: `remove_listener` in a `finally`, or clear buffers per `open()`.
- **F5 (partial)**: default `fingerprint=True` / set a non-headless UA when stealth is on.
- **F7**: bare-`%Y` parse + nested dotted-path date resolution.
- **F8**: replace the bool-first rank with a weighted score.
- Debug-log the silent XHR-parse drop (`dom.py:74`) and the F9 total-rejection.

**Larger refactors**
- **F2/F4**: make the executable artifact genuinely self-contained — carry the full
  `Resolve` (proxy/antibot included) into the lazy `resolve()` op / the blob, and test the
  shipped artifact end-to-end so `tested` means what it says. This touches the reference/
  resolve lazy encoding and `run_query`.
- **Stealth realism (F5)**: a consistent-fingerprint pass (UA ↔ platform ↔ GPU ↔ engine
  version) rather than the current independently-spoofed knobs.
- **Signals**: introduce negative/contra evidence so structural chrome (`forms`/`buttons`/a
  lone `attachShadow` mention) can't one-directionally force browser escalation.

---

## Appendix — reproductions (run against `env/`, Python 3.12)

- **F1 (RCE):** `_parse_query("wq.reference.__globals__['__builtins__']['__import__']('os').system('echo PWNED > /tmp/pwned')")` wrote `/tmp/pwned` before raising its post-hoc `TypeError`.
- **F3 (false success):** an LLM stub returning an always-incomplete query made `write_query`
  return `row_count=1`, `sample=[{'title':'A','date':None}]`; `_onboard_company` would set `ok=True`.
- **F5 (UA leak):** default `headless=True` chromium → `HeadlessChrome/151…` in `navigator.userAgent`.
- **Selectors (no bug):** `:has()`, `:scope + p`, and `//p[a[contains(@href,'/news/')]]`
  from the skill all extract correctly against the live `Document` engine.
