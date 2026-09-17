# Deep review — `webclient/pipelines/` onboarding pipeline

Scope: `onboarding.py`, `llm.py`, `__main__.py`, `prompts/`, `guides.py`,
`skills/lazy-queries.md`. Core library (core/signals/clients) explicitly out of scope.
Reviewed by reading the code and reproducing the concrete failures where noted.

Findings are ranked by severity. Line numbers are against the files as reviewed.

---

## HIGH

### H1 — Timeliness gate judges only the 5-row sample, in DOM order
`onboarding.py:1864` `review_query` calls `_timeliness(list(q.sample), brief)`, and
`q.sample` is `good[:5]` (`write_query`, `onboarding.py:1654`). So the timeliness
verdict — a **deterministic, hard FAIL** (`onboarding.py:1876-1883`) — is computed over
at most the first five extracted rows, taken in the order the record selector matched
them (DOM order), not sorted by date and not the full row set.

Failure scenario: a correct query against a listing that is ordered oldest→newest (or
simply not newest-first — e.g. an alphabetical catalogue with a date column, a changelog
rendered ascending, a page where the hero/featured item is old). The newest record sits
past row 5, so `dates[0]` in the sample is an old date, `age` is large, and the gate
marks the run stale and fails it — sinking an otherwise fully-correct onboarding. The
inverse false-pass also exists (a genuinely stale page whose first 5 rows happen to be
its most recent). The row cadence (`statistics.median(gaps)`) is also estimated from ≤4
gaps, which is noisy.

Mitigation today: most of the dated briefs (ir-news, blog, changelog, github-releases,
podcast-episodes) list newest-first, so it usually works — but nothing enforces that and
the gate is unforgiving when it doesn't hold.

Fix: run timeliness over the **full** extracted row set, not the truncated sample.
`QueryArtifact` only stores `sample` (≤5), so either (a) store all extracted dates / a
larger sample dedicated to timeliness, or (b) sort rows by parsed date descending before
taking the newest, and compute cadence over all rows. At minimum, take the max date over
all rows rather than `dates[0]` of a DOM-ordered 5-row slice.

### H2 — Nested required fields are never validated for content (`complete=True` with empty nested values)
`_populated_rows` (`onboarding.py:1478-1490`) and `_empty_required_fields`
(`onboarding.py:1506-1514`) only look at **top-level** columns, and treat any non-empty
container as populated (`_nonempty`, `onboarding.py:1466-1475`: a dict/list is "non-empty"
when `len>0`). `_required_columns` (`onboarding.py:1493-1503`) collapses a nested path to
its top segment (`price.value` → `price`).

A nested column is produced by a sub-`extract(...).project()`, so it is always a dict with
keys, e.g. `{"value": "", "unit": ""}` — structurally non-empty even when every leaf is
blank. Consequences in `write_query` (`onboarding.py:1644-1652`):
- `good = _populated_rows(rows)` counts a row `{"name":"X","price":{"value":"","unit":""}}`
  as a real row.
- `missing = _empty_required_fields(good, brief)` sees `price` present (dict, len 2) → not
  missing → returns `[]`.
- `complete = tested and good and not missing` → **True**, and `row_count` is inflated.

So a query that matches the record wrapper and the nested container but extracts none of
the nested leaf values is accepted as a complete success and shipped. This defeats the
whole point of the content-validation retry loop for any brief with a nested schema
(product_catalogue price object, etc.). Flat schemas are validated correctly; only nested
ones are blind.

Fix: make `_nonempty` recurse — a dict/list is "populated" only if it contains a
non-empty leaf. And check required **leaf paths** (via `_dig`) rather than only the top
column name, so an all-blank nested branch counts as missing.

---

## MEDIUM

### M1 — `_json_blob` brace/bracket scanner is not string-aware
`onboarding.py:578-596` extracts "the first balanced JSON object/array" by counting
`open_ch`/`close_ch` with no awareness of string literals or escapes. A `}`/`]` (or an
unbalanced `{`/`[`) inside a JSON **string value** miscounts depth and returns a truncated,
invalid blob. Reproduced:

```
{"verdict":"poor","summary":"the } brace ends it","score":2}
  -> _json_blob returns  {"verdict":"poor","summary":"the }   → json.loads FAILS
```

This is not exotic: the models routinely put braces/brackets in `summary`/`verdict`/
`why`/`reason`/`note` and in selector strings (`[class*="price"]`, `a[href]`, `{ }`).
When it fires, `_ask_json` (`onboarding.py:599-622`) returns `None` after one retry
(the retry re-runs the same broken scanner on the new reply), so the whole stage silently
degrades: `select_candidates` returns fewer/zero picks, `_pick_edges` returns `[]`, a
review is dropped, or the raw-blob fallback `from_blob(_json_blob(reply))` in
`_parse_query` (`onboarding.py:1378`) throws.

Fix: track in-string state and backslash escapes in the depth scan, or use
`json.JSONDecoder().raw_decode` starting at the first `{`/`[` and let the real parser find
the extent.

### M2 — `_parse_date` cannot parse RFC822 feed dates → timeliness silently skipped for RSS/Atom
`_parse_date` (`onboarding.py:1765-1784`) handles named-month, numeric-slash and ISO
`YYYY-MM-DD` (via a regex) forms, but **not** RFC822 (`Tue, 18 Dec 2025 10:00:00 GMT`),
which is exactly what RSS `<pubDate>` uses. RSS/XML feeds are a first-class target of this
pipeline (case-insensitive XML selects, `.as_json()`, the `_DATA_DOC_HINTS` for
`.rss`/`.atom`/`/feed`). For any RSS feed, `_date_field_paths` finds a date field, but
`_parse_date` returns `None` for every row, `dates` is empty, and `_timeliness` returns
`("", False)` — the timeliness gate is silently a no-op on the very sources where "is the
latest item present" matters most. (Atom's ISO `2025-12-18T...` is caught by the fallback
regex; RFC822 is not.)

Fix: add RFC822 parsing (`email.utils.parsedate_to_datetime`) and an ISO-with-time format
to `_parse_date`.

### M3 — Chosen source is fetched 2–3× (wasted cost/time under browser resolve)
For the finally-chosen source the pipeline fetches the same URL repeatedly:
1. `evaluate_candidate` — `wc.fetch(candidate.url, …)` (`onboarding.py:1076`).
2. `_onboard_company` — `wc.fetch(query_url, …)` to read flags (`onboarding.py:2047`),
   stored as `artifacts.query_doc`.
3. `write_query` — `wc.fetch(candidate_url, …)` again (`onboarding.py:1596`).

When there is no separate `api_endpoint`, `query_url == candidate.url`, so the source is
fetched three times in one run. Each fetch may force a browser render + proxy/stealth
(the whole reason `write_resolve` exists), so this is real spend and latency, and can also
produce skeleton drift between the flags fetch (used by `review_query`) and the authoring
fetch (used by `write_query`) on a nondeterministic SPA.

Fix: thread the already-fetched `artifacts.query_doc` into `write_query` (accept an
optional `doc=`), and reuse the evaluate fetch where the URL is unchanged.

### M4 — `_review_from_json` treats a stringy `"pass"` as passing → a FAIL review can silently pass the gate
`onboarding.py:1692`: `passed=bool(data.get("pass", True))`. The cheapest model has high
formatting variance and sometimes emits `"pass": "false"` / `"no"` / `"0"` as strings.
`bool("false")` is `True`, so a review the model intended as a **fail** passes the gate
(`_gate`, `onboarding.py:1906-1922`) and the pipeline ships a source the reviewer rejected.
(The default `True` when the key is absent is intentional and documented; the bug is the
non-bool coercion.)

Fix: coerce explicitly — treat only real `true`/`1`/`yes` as pass, and any recognised
false token (`false`/`no`/`0`) as fail; be conservative on gates that block.

### M5 — Timeliness / review gates are inert on a normal run
All gating — the crawl, select and query reviews, and therefore the deterministic
timeliness FAIL — only runs under `review=True` (`onboarding.py:2018, 2041, 2082`), which
the CLI sets only with `--review` (`__main__.py:95`). By default `onboard()` /
`onboard_company()` do **not** enforce timeliness or any review, yet `result.ok` is set
purely from `q.complete` (`onboarding.py:2069`). A run can therefore report `ok=True` and
ship a stale/wrong-but-populated query. Given how prominently the module docstrings and
prompts frame timeliness as a gate "so we don't ship a wrong or stale dataset", the fact
that it is off unless opted in is a trap.

Fix: consider running the deterministic timeliness check (H1/M2 fixed) unconditionally —
it needs no LLM call — and folding it into `result.ok`, independent of the opt-in LLM
review stage. At minimum document loudly that timeliness requires `--review`.

---

## LOW

### L1 — Query AST interpreter permits terminal execution ops; the "no code execution" claim is broader than what's enforced
`_eval_query_ast` / `_parse_query` (`onboarding.py:1306-1378`) correctly block Python-level
RCE (only `wq`, no `_`-attrs, literals + query operators). But it evaluates the **whole**
chain by actually invoking the wq calls, and nothing forbids terminal ops. Because `wq` is
the full surface (not just `wq.doc`), an injected reply such as
`wq.reference("http://internal/…").resolve().collect()` is parsed and its `.collect()` is
executed during authoring. Whether it performs a real fetch depends on client binding
(a `wq`-rooted expr may have no engine and raise first), so this is defense-in-depth rather
than a confirmed SSRF — but the docstring's guarantee ("a hostile crawled page cannot
achieve code execution") is stated more absolutely than the enforcement. Crawled page
content does flow into the authoring prompt (skeletons), so prompt-injection steering the
model here is plausible.

Fix: restrict the interpreter to the recording ops actually used (reject
`collect`/`acollect`/`stream`/`execute`), and/or root authoring at `wq.doc` only rather
than the whole `wq` surface. Keep relying on `Plan.validate_names` too.

### L2 — `_DOCS_HINTS` hard-bans some legitimate data paths irreversibly
`_DOCS_HINTS` (`onboarding.py:807-811`) includes `/reference/`, `/guide`, `/sdk`, `/help/`.
`_is_docs_url` is a **hard** ban: such a URL is dropped from the frontier and can never
become a candidate (`_filter_frontier` `onboarding.py:875`, `select_candidates`
`onboarding.py:1011`). A dataset legitimately under e.g. `/reference/countries` or a
`/guide/...` data page is silently unreachable. The comment claims specificity, but
`/reference/` and `/guide` are broad.

Fix: soften the ambiguous fragments to a deprioritise (push down the frontier) rather than
a hard ban, or require the docs host-prefix (`docs.`/`developer.`) for the borderline
fragments.

### L3 — Paginated authoring injects a page-level `next` link as a per-record field
The `paginated` branch (`_query_prompt`, `onboarding.py:1208-1212`) tells the model to
extract the `rel="next"` anchor "as a field named `next`". That link is page-level, so it
ends up duplicated on every record row, pollutes the output schema, and inflates
`_populated_rows` (a row whose real fields are all empty still counts as populated because
`next` is set) — which can nudge `row_count`/`good` upward misleadingly.

Fix: capture `next` once at page level (separate from the row extraction), or exclude a
known `next` column from the populated-row test.

### L4 — Tested sample can diverge from the shipped blob when the model prefixes a stray top-level `resolve`
`_test_query` runs the full authored expr against `doc` (`onboarding.py:1546-1557`), while
`_executable_query` ships only `_extraction_steps` — steps from the first `select`/
`select_all` onward, dropping any leading top-level navigation (`onboarding.py:1242-1251,
1271`). If the model ignores the prompt and prefixes a top-level `.resolve()`/navigation,
the sample/`tested` reflect the navigated page but the shipped blob extracts from the
original — a silent mismatch. Rare (the prompt forbids it), hence low.

Fix: test the same steps that get shipped (run `_executable_query`'s extraction against
`doc`), or reject a top-level pre-`select` navigation with feedback instead of silently
stripping it.

---

## Prompts & query guide (`skills/lazy-queries.md`) — correctness for one-shot authoring

Overall the guide is strong: three-move structure, durable-selector guidance, JSON vs HTML
distinction, the no-wrapper/`:scope + p`/`:has()` cases, nested-resolve, and the
injected-JSON-island `.as_json()` example are all coherent and match the DSL the
interpreter accepts (operators, `optional=True`, `is_ok`/`is_empty`, XPath). The op
reference is generated live from docstrings (`guides.py`), so it can't drift. No wrong
examples found. Minor notes:

- The guide (Example 3 / RSS note) tells the author XML tag names are **case-sensitive**
  and to "match `pubDate`, not `pubdate`, exactly". Recent work added a **case-insensitive
  fallback for bare XML tags** (commit context). The guide is now stricter than the engine;
  harmless (the exact case still works) but the two messages are slightly inconsistent — a
  model that trusts the guide will still succeed, but the "must match exactly" wording is no
  longer strictly true.
- `write_query.md` says root at `wq.doc` and "do NOT write a fetch/resolve/reference," yet
  the guide's nested-resolve example uses a per-record `.resolve()`. This is fine (the
  per-record resolve is inside `extract`, not a top-level fetch) but a cheap model may read
  the two as contradictory; a one-line clarification ("`.resolve()` is allowed *inside* a
  field to follow a record's link; just don't fetch/resolve the whole page") would help.
- The pager instruction (L3) asks for `next` "as a field named `next`", which conflicts
  with the guide's per-record framing.

These are clarity nits, not defects.

---

## Design / clarity notes (non-blocking)

- `run_query` (`onboarding.py:1560-1573`) swallows every per-base exception with a bare
  `continue`, so a dataset split across bases can silently drop whole bases with no trace.
  Consider logging the failing base.
- `_candidate_score` uses a fixed `+4` queryable bonus (`onboarding.py:1159`); a
  scrapability-5 queryable API ties a scrapability-9 clean page. Fine, but the constant is
  a magic number worth naming.
- `evaluate_candidates` early-stops on the first `usable and is_queryable`
  (`onboarding.py:1148-1149`) — it won't keep looking for a *cleaner* queryable source once
  any queryable one clears the bar. Acceptable cost trade-off, worth a comment.
- `DEFAULT_MODEL = "claude-opus-5"` (`llm.py:57`) but `cheapest_model()` is what the CLI
  actually uses by default (`__main__.py:41`); the "default" constant is somewhat
  misleading given the CLI never uses it unless `--model` is given.

---

## Summary of fixes, most valuable first
1. **H1**: compute timeliness over all rows (or date-sorted), not the DOM-ordered 5-row sample.
2. **H2**: recurse `_nonempty` and check required leaf paths so empty nested branches fail `complete`.
3. **M1**: make `_json_blob` string/escape-aware (or use `raw_decode`).
4. **M2**: parse RFC822/ISO-with-time dates so timeliness works on RSS/Atom.
5. **M3**: reuse the already-fetched source doc in `write_query` instead of re-fetching.
6. **M4**: coerce `"pass"` robustly so a stringy false actually fails the gate.
7. **M5**: make the deterministic timeliness check run without `--review`, or document that it doesn't.
