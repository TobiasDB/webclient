# webclient core — deep review

Scope: `webclient/core/` (web_core dispatch, document/, reference/, crawl/, session/, remote/,
client/), `webclient/signals/`, `webclient/clients/`, `webclient/collection.py`,
`webclient/query/`, `webclient/models.py`. Pipeline excluded (covered separately).

Method: read the code; two findings verified by reproducing the logic (`_is_noise_class`,
JSON `select_all` policy path). Findings ranked by severity.

---

## Critical / High

### H1. JSON `select_all` raises on a missing path — breaks the "empty match is still a collection" contract
`webclient/core/document/json.py:134-148`

```python
def select_all(self, core, path, *, limit=None, offset=0):
    node = self.select(core, path)          # <-- default error=None
    data = None if node._missing else node._element
    ...
```
`JsonBacking.select` with `error=None` falls through to `_miss(core, msg, None)`, and
`_miss` does `if (None or current_policy()) is RAISE: raise`. The ambient policy is `RAISE`
(default, see below — nothing ever sets it to `RETURN`). So on a **missing key/path**,
`json_doc.select_all("missing.items")` **raises** `SelectError`.

`HtmlBacking.select_all` (html.py:823) calls `_find` directly and returns `[]` on no match —
it never raises. So the two backings disagree on the *same op*: the html surface honours
"an empty match is still a collection" (its own docstring, json.py:141 too), the json surface
does not.

Impact: this is the new `select("script#…").as_json().select_all("items")` island path, and
any JSON-API crawl/extract. A typo'd path, or a page whose JSON simply lacks the array,
crashes the whole fan-out (a `fan_out` failure cancels its siblings) instead of yielding an
empty collection. Reproducible with the default policy — no `RAISE` opt-in needed.

Fix: make `select_all` tolerant regardless of policy, e.g.
`node = self.select(core, path, optional=True)` (then treat `_missing`/non-list as empty), so
it matches html's "empty match is a collection" behaviour. (Note the current code already
handles a non-list resolved value as empty; only the *missing-key* path is loud.)

---

## Medium

### M1. `_is_noise_class` drops legitimate semantic classes (skeleton quality)
`webclient/core/document/html.py:223-239`

The high-entropy heuristic has real false positives. Verified by running the exact logic:

| class | dropped as "noise"? |
|---|---|
| `heading2` | **yes** |
| `section2` | **yes** |
| `level3heading` | **yes** |
| `container2` | **yes** |
| `results2024` | **yes** |
| `USMap` | **yes** (3 uppers) |
| `ProductCardItem` | **yes** (3 uppers) |

Two rules over-fire:
1. `uppers >= 3` flags any PascalCase class with 3+ capitals (`ProductCardItem`, `USMap`) —
   CSS-module / React className conventions produce these as *stable* hooks.
2. the `[-_]`-segment rule flags any ≥6-char segment mixing a letter and a digit, so a
   trailing number (`heading2`, `section2`, `container2`, `results2024`) is treated as a hash.

Consequence: `skeleton()` silently removes these class tokens from the outline an LLM writes
selectors against, so a selector like `.heading2` / `.ProductCardItem` is never suggested and,
if guessed, looks unsupported. This directly undercuts the skeleton's stated purpose.

Fix: tighten the digit rule to require the segment to look like a *hash* (mixed case AND a
digit, or ≥2 digit runs), not just "contains one trailing digit"; and don't treat PascalCase
alone (no digit) as noise — require a digit or the known CSS-in-JS prefixes. e.g. keep a token
that is `word + short numeric suffix` (`heading2`, `col2`).

### M2. `LiveBacking.select_all` signature omits `limit`/`offset` — `TypeError` on a live page
`webclient/core/document/live.py:328` vs the generated surface (`models.py:268`,
`collection.py:197`)

The typed surface is `select_all(self, selector, *, limit: int|None=..., offset: int=...)`,
and `HtmlBacking.select_all` implements it. But `LiveBacking.select_all(self, core, selector)`
takes neither kwarg, and `LiveBacking` wins dispatch whenever `core._page is not None`
(it precedes `HtmlBacking` in `Document.BACKINGS`). `dispatch` forwards kwargs verbatim
(`getattr(backing, op)(self, *args, **kwargs)`), so on a `keep_alive` browser document
`doc.select_all("a", limit=5)` raises `TypeError: select_all() got an unexpected keyword
argument 'limit'`. `LiveBacking.select` matches its html twin; only `select_all` drifted.

Fix: give `LiveBacking.select_all` the same `*, limit=None, offset=0` signature and apply the
slice after `loc.count()`.

### M3. Crawl fetches serially — the step lock is held across all fetches in a round
`webclient/core/crawl/backing.py:106-125` (`_pump`)

`_pump` runs the whole round under `async with self._lock(core)`, and the fetch loop is a
sequential `for edge in to_fetch: page = await self._fetch_edge(...)`. So a `run()` (and each
`step()`) fetches **one page at a time**; `config.width` only bounds how many edges are
*claimed* per round, never how many fetch concurrently. The page pool / http concurrency and
the executor's `fan_out` are entirely unused by a crawl. On top of that, `_astream` yields a
round's pages only after the whole round (all its fetches) completes under the lock, so
streaming is not incremental within a round.

Only the frontier-claim + budget accounting needs the lock; the fetches do not.

Fix: hold the lock only for `choose()` + the frontier mutation (claim `to_fetch`, mark seen),
release it, then fetch the claimed edges concurrently via `fan_out(to_fetch, self._fetch_edge,
limit=width)`; take the lock again briefly to append pages / expand the frontier (or make
`_add_edge`/`pages.append` themselves lock-guarded). This is the single biggest core
throughput issue.

### M4. `BrowserClient.open` leaks page event listeners on any mid-`open` exception
`webclient/clients/browser.py:248-318`

`page.on("console", …)` / `page.on("request", …)` are registered near the top; the matching
`page.remove_listener(...)` calls are the *last two statements* of `open`, with no
`try/finally`. Any exception between them — `page.goto`, `_do_wait` raising under
`on_timeout=RAISE`, an `evaluate` in the inline/drain/load phases, a replay step — returns
without removing the listeners. The code's own comment (browser.py:237-238) explains why this
matters: "a page can be reused from the pool, and re-adding anonymous listeners each open()
would pile up and multiply the console/network events (which feed SPA detection)". Reuse
happens on `reload()` (`_alive(ref, replay=...)`) and any keep-alive page re-driven through
`open`. Accumulated listeners double-count console/network events → inflated SPA/XHR signals.

Fix: wrap the body in `try/finally` and remove both listeners in the `finally`.

### M5. Aggressive locale folding in `_canon` can dedup two *distinct* pages
`webclient/core/crawl/canon.py:47-115, 152-178`

`_strip_locale_path` / `_dedup_host` fold any leading path segment or subdomain that is a
locale code. But the code list includes short, ambiguous tokens that are also real content
segments — e.g. `ca` (Canada / Catalan), `is` (Icelandic), `in`? (not listed, but `id`, `it`,
`no`, `us` are). So `/ca/products` and `/products`, or `/is/pricing` and `/pricing`, collapse
to one canonical key. Because `_canon` is the crawl dedup key, the second distinct page is
silently dropped from the frontier (`_add_edge` returns early on a seen key).

This is a deliberate tradeoff for multi-region sites, but it *loses pages* on any site that
uses one of these tokens as a genuine section. Consider only folding a leading segment when a
sibling non-locale copy is actually observed, or restrict the fold to `lang` or `lang-country`
forms (`en`, `en-us`) and drop the bare 2-letter country codes from path folding.

---

## Low

### L1. Stale/contradictory policy comment; `default_policy` is dead code
`webclient/errors.py:34-37,53` and `webclient/collection.py:121-148`

The errors module comments that "extract/filter run their sub-expressions under RETURN so one
bad field never aborts a whole plan," and provides a `default_policy(...)` context manager —
but `grep` shows `default_policy` is **never called anywhere**, and `current_policy()` always
returns the module default `RAISE`. So extract/filter are in fact **loud** (matching
`apply_extract`'s own docstring: "Loud by default … raises"). The behaviour is self-consistent
(loud), but the errors-module comment describes the opposite and the context manager is unused.
Either wire `default_policy(RETURN)` around `apply_extract`/`survives_filters` (if lenient
extraction was intended) or delete the dead helper and fix the comment.

### L2. `_expire_page` fire-and-forget task can be GC'd
`webclient/core/client/__init__.py:857-871`

`asyncio.ensure_future(_expire())` — the returned task is not stored anywhere. asyncio keeps
only a weak reference to a task, so a keep-alive-with-TTL page's safety-net release can be
garbage-collected before it fires, defeating the leak guard it exists for. Keep a strong
reference (e.g. a `set` on the client, discarded in a done-callback).

### L3. `_find` XML case-insensitive fallback can over-match across namespaces / miss the root
`webclient/core/document/html.py:793-802`

The fallback `.//*[translate(local-name(),…)=$t]` matches *every* descendant with that
local-name regardless of namespace prefix, so a bare `date` matches `dc:date`, `atom:date`,
etc. That is usually the desired leniency, but it can silently return elements from an
unintended namespace when the feed mixes them. Separately, `.//*` excludes the context node
itself, so a bare-tag select that names the *root* element (`select("rss")` /
`select("feed")`) can't match via the fallback. Both are edge cases; document the local-name
semantics, and if root-matching is wanted use `descendant-or-self::*`.

### L4. `as_json` doesn't carry render context; JSON dotted-path has no negative index
`webclient/core/document/html.py:653-673`, `json.py:167-189`

`as_json` builds the new json Document with `content`/`kind`/`url` but not `_static_html` /
`_render_stats` / `_set_cookies`, and when `core._element is None` it reparses the *entire
document text* as JSON (harmless — yields a not-ok doc — but surprising). Minor. Also the json
path tokenizer `re.findall(r"[^.\[\]]+|\[\d+\]", path)` only accepts non-negative bracket
indices; `items[-1]` is treated as key `"-1"` → miss. Fine to leave, worth a docstring note.

### L5. Streaming degrades to eager for a terminal *property* element op
`webclient/query/executor.py:376-398` (`_stream_tail`)

`_stream_tail` requires the last step to be `kind == "call"`, so a plan ending in a property
op (`…select_all(…).text_content`, `.region`, `.title`) is not recognised as a streamable tail
and falls back to evaluate-then-yield (materialise all, then hand out). Common terminal props
(`text_content`) therefore never stream. Consider treating a terminal `get` whose name is in
`Document.prop_ops()` as a streamable per-element tail.

---

## Notes / non-issues (checked, OK)

- `web_core` dispatch/backing selection, `choose()` ordering (registered backings before
  built-ins), memoised op tables, remote-vs-local `_goes_remote` gating — consistent.
- `ClientPool.release` is idempotent and returns the permit in a `finally`; `lease` releases
  the permit on create-failure. `pages_free = limit - held` cannot go negative in practice
  (held is bounded by the semaphore; the `_semaphore` default-10 vs `_free`/`_total` default-0
  only diverges for an *unconfigured* kind, and the client always sets `page`/`http` limits).
- `fan_out` / `fan_out_stream`: order-preserving vs completion-order, sibling-failure notes,
  cancellation in `finally` — correct. `_plan_scope` / `_per_element` page-lease release and
  the deadlock rationale are sound.
- Noisy-OR + contra combination in `build_flag` (positives via `_combine`, each contra
  multiplies `(1-c)`) is correct; contra-only groups collapse to 0.
- `EngineLoop` teardown (`_stopping` guard, bounded drain rounds, re-entrancy guard) and the
  sync/async stream bridges look carefully done.
- http `send` retry covers `httpx.TimeoutException` (a `TransportError` subclass); Set-Cookie
  aggregation across redirect hops via httpx's parsed jar is correct.
- Session cookie absorb, shared engine/pool/bus, per-host pacing on the shared engine — OK.
- Remote (de)serialization round-trips (`__doc__`/`__ref__`/`__model__` tags, `_wire_models`,
  crawl state adoption) are symmetric with the local result types.
