# Running a query — one interface (plan)

Decisions (user, 2026-10-01): running lives INSIDE web.dsl; the schema is optional and carried in
the plan; loud failure is the default; one interface whether or not a schema is present; the
report carries everything; a detail row inherits its parent row; streaming remains; identity's
default is baked into the Document, not the DSL or onboarding.

## The objects

- `Schema` (optional, in the `Plan`): the fields (name, type, description, optional, document),
  the identity rule (parts), `expect_rows`, a `cadence` hint. A brief fills it; an ad-hoc query
  has none and still runs.
- `Query` (web.dsl, replacing the onboard alias): `from_blob` / `from_source` / `from_plan`,
  `to_blob` / `describe` / `to_source`, `.schema`, `.with_schema(...)`, `.nest(field, child)`.
  The lazy `wq` chains stay as they are; a `Query` is the executable, portable form of one.
- `Run`: `rows`, `documents`, `report` — produced by `await query.run(resolver, sink=…,
  lenient=False)`; `async for event in query.stream(...)` yields rows / documents / issues /
  fetches as they happen, the report accumulating (sinks receive the same stream).
- `Report`: row count (and the schema's expected range verdict), per-field fill rates, duplicate
  identities, newest-row age against the cadence (datetime-typed fields), per-row issues (lenient
  mode), every fetch (url, tier, status, elapsed), failures. Mechanical; what a review reads first.
- `Sink` / `Dataset` / `MemorySink` move to `web.dsl.sink`; `Dataset` is the default sink.

## Policies

- Loud by default: a required field's selector that misses raises, as today. `lenient=True`
  records a per-row issue (`_issues`) and continues — what a production run and the author's
  probe use.
- Nested: `parent.nest("url", child)` — at run time each parent row's `url` resolves the child
  query over that document; each child row INHERITS the parent row (its columns copied in, its
  `_identity` composed from the parent's) and streams as produced. Onboarding's nested seam
  (`AuthorReview.next = "nested"`) builds this.
- Identity: `Document.identity()` / `Element.identity()` (web.parse) — the content digest, or a
  declared rule — is the default; a row's identity is the digest of its extracted fields, else the
  record element's identity. `digest` moves to web.parse; the DSL calls it, never computes it.

## CLI

`web run <blob | state.json> [--lenient] [--sink memory|dir] [--sample N]` — runs and prints the
report; `web view` shows the report of the last run on a state.

## Steps (each a green commit)

1. `Schema` in the plan + `Query` in web.dsl (onboard's `compile.Query` alias retired).
2. `Run` / `Report` + `stream()` + sinks moved into web.dsl; `run_to_sink` retired.
3. identity onto Document / Element (web.parse); the DSL reads it.
4. lenient mode (per-row issues) — the author's probe uses it instead of rewriting selectors.
5. nested queries (parent row inherited; streamed).
6. onboarding switched: the brief fills the schema; stage 8 / 9 read the report; `web run`.
