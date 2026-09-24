# Playground — user stories and UX brief

*Status: proposed 2026-09-24. Feature B of two (the other is the product website). Scope:
the interactive application over the whole package — explore a page, build a query,
steer a crawl, drive a live browser, onboard a dataset with a human in the loop, and
record / replay / debug everything — for technical and non-technical users alike. This
replaces the current `/ui` (a functional but unstyled trace viewer) and needs heavy UX
design before code.*

## 1. Purpose

The library's ideas — plans as data, cheapest-tier transport, signals with evidence,
loops with checkpoints, traces you can replay — are invisible in a terminal. The
Playground makes them **manipulable**: a user should be able to go from "here is a URL"
to "here is a tested, scheduled query" without writing code, watch every decision the
engine makes as it makes it, take over when the auto mode is wrong, and replay any run
to see why. Every action in the Playground produces the same artefacts an engineer would
(a plan blob, a trace, a brief), so nothing done here is a dead end.

## 2. Personas and their jobs

| Persona | Job to be done | Success looks like |
|---|---|---|
| **Analyst (Ana)** — non-coder, needs a table from a page today | paste URL → rows → download / share | rows in < 2 min, no selector ever seen |
| **Data engineer (Dana)** | build durable extractions; page through datasets; schedule; monitor drift | a blob that re-runs identically; alerts when it breaks |
| **Agent developer (Ben)** | see what a model would see (skeleton, element table, flags), test tools, debug an agent run | a tool call reproduced from a trace; errors with remedies |
| **Operator (Oli)** | run for a team; watch load; find why a run failed at 3am | a trace of every run; resources per session; replay offline |
| **Reviewer (Rae)** — domain expert asked to confirm a source | answer "is this the right dataset?" from the chosen page + sample rows | a yes/no with context, in one screen |
| **Learner (Lee)** — evaluating the product on the website | try the feature pages' demos with their own URL | the site's demo opens in the Playground with state loaded |

## 3. Information architecture (workspaces)

```
Playground
├─ Home            recent runs · saved queries · traces · "start with a URL"
├─ Explore         a URL → card · flags (evidence) · patterns · skeleton · markdown · element table · events
├─ Query           the visual query builder: pick records/fields → live rows → plan blob / wireframe / explain
├─ Crawl           seeds · scope · frontier map · steer / auto / locate-until · pages as cards
├─ Interact        a live browser page: element table · actions · record → plan · replay
├─ Onboard         brief → pipeline (stages, gates, reviews) → confirm gate → tested blob
├─ Runs            every execution: status · rows · trace link · re-run · schedule (P2)
├─ Traces          timeline · snapshots · rrweb replay · HARs · ledger · resources · replay modes
├─ Tools           the registry: try any tool, see schemas, copy MCP/HTTP/Python
└─ Settings        scripts (toggle/policy) · drivers · resolve policies · sessions · limits · tokens
```

Cross-cutting: a **command palette** (⌘K) to jump/act; a persistent **run bar** at the
bottom showing the live event stream for the current workspace; every screen has a
**"as code"** drawer (the Python / HTTP / MCP that reproduces what the screen did).

## 4. Story map

Priority: **P1** launch · **P2** next · **P3** later. Stories carry UX notes and the API
they need (existing endpoints are named; new ones are marked *new*).

### Epic 1 — Explore a page (the first 60 seconds)

- **1.1 (P1)** As Ana, I want to paste a URL and, in one screen, see what the page is
  (card), what's notable (flags with evidence and remedy), where the data is (pattern
  hints highlighted on a rendered preview), so that I know what to do next. *UX:* a
  three-column "Explore" — preview (the snapshot, hints highlighted on hover) · findings
  (card, flags as chips with evidence popovers) · next actions ("Extract this list", "This
  needs a browser", "This needs a login"). *API:* `/tools/card|flags|patterns|skeleton`.
- **1.2 (P1)** As Ben, I want the skeleton and the element table side by side with the
  preview, with hover-linking (hover a line → outline the element), so that I see what a
  model sees. *UX:* monospace panes with sticky legend; click copies a selector.
- **1.3 (P1)** As Eva/Lee, I want to choose the transport (static / auto / always) and
  see the escalation trail with timing and the flags that decided each hop, so that the
  ladder is understandable. *UX:* a horizontal "tier ladder" with the hop taken lit.
- **1.4 (P2)** As Ana, I want markdown/text views with a copy button and a word count,
  so that I can use the page as text. *API:* `/tools/fetch_markdown|fetch_text`.
- **1.5 (P2)** As Ben, I want the page's network events (XHR/data APIs) and the
  XHR→DOM correlation shown on the preview ("this region came from request #3"), so that I
  can pick the API instead of the page. *API:* trace events; `xhr_endpoints`.
- **1.6 (P3)** As Ana, I want to compare two URLs (or two tiers of one) side by side.

### Epic 2 — Build a query without writing one

- **2.1 (P1)** As Ana, I want to click a repeating region in the preview (or accept the
  top pattern hint) to choose the records, then click fields inside one record to name
  columns, and see rows fill live, so that I build an extraction by pointing. *UX:* the
  record is outlined with a "× 24" badge; clicked fields get coloured chips; a rows table
  updates on every change; a "0 rows" state explains why (the ledger's hint). *API:* the
  query loop's `record_options` / `field_options` (index-based; the UI never generates a
  selector), `/execute`.
- **2.2 (P1)** As Dana, I want to see the plan as a wireframe, an explain tree and a blob,
  and copy it / download it, so that the query is an artefact. *API:* `/plan` (wireframe).
- **2.3 (P1)** As Dana, I want to add pagination (rel=next / page param / cursor) with a
  max pages and a stop rule, and preview rows across pages with a progress indicator, so
  that the whole dataset is captured. *UX:* the pagination flag suggests the mode.
- **2.4 (P2)** As Dana, I want attribute values (href, datetime, data-price) offered when
  a field lives in an attribute, and a regex refiner with live preview, so that values are
  clean. *UX:* the value-attribute chips from the skeleton surface here.
- **2.5 (P2)** As Dana, I want to test the query against a second URL of the same kind
  (a detail page, tomorrow's listing) and see the diff, so that I trust its durability.
- **2.6 (P2)** As Dana, I want "let the model author it" (the index author) as a one-click
  alternative and to compare its query with mine, so that I can use an LLM without
  handing over control. *API:* `build_query` with a server-side model.
- **2.7 (P3)** As Dana, I want a typed output schema (column types, required fields) with
  validation counts per run.

### Epic 3 — Crawl and locate

- **3.1 (P1)** As Dana, I want to seed a crawl, set scope/limits, and watch the frontier
  as a map (pages as nodes, edges scored, resources dropped, robots-blocked marked), so
  that I understand what it will and won't fetch. *UX:* a force/tree graph with a
  sortable frontier table beside it; each page node opens Explore. *API:* the crawl over
  `/execute` + LoopEvents on the stream; *new:* a server-held crawl session
  (`POST /crawls`, `step`, `resume`).
- **3.2 (P1)** As Dana, I want to switch between auto (best-first), manual (I pick edges
  each round) and locate ("stop when a page matches …"), and to interrupt an auto crawl
  and pick by hand mid-run, so that steering is possible. *UX:* a round-by-round stepper;
  the driver's Ask appears as a decision card with the frontier ranked.
- **3.3 (P2)** As Dana, I want sitemap/robots discovery as a first step with the URLs
  previewed and selectable as seeds.
- **3.4 (P2)** As Dana, I want the crawl's pages projected (card by default, or my
  query) into a table I can export.
- **3.5 (P3)** As Dana, I want a saved crawl resumed from its state.

### Epic 4 — Interact with a live page

- **4.1 (P1)** As Ben, I want a live browser page (screenshot stream or the rrweb live
  mirror) with the element table beside it, and actions (click / type / wait / scroll /
  goto) by clicking the table or the mirror, so that I drive a page without a selector.
  *UX:* every action appends a step to a visible sequence; the page's console and network
  stream in the run bar. *API:* *new:* a server-held live page session with actions over
  ws; the recorder (`wc.record`) behind it.
- **4.2 (P1)** As Dana, I want "record" to capture my steps as a plan, replay it, and
  hand the reached page to Query, so that a login-then-extract flow is one artefact.
- **4.3 (P2)** As Ben, I want the interaction loop (a model policy) to run with me
  watching, pausing on stalls or on my request, so that I can take over. *UX:* the loop's
  round/decision events as a step list with "pause / take over / resume".
- **4.4 (P2)** As Ben, I want scripts toggled per session (rrweb on/off, custom probes)
  and their results shown, so that instrumentation is visible.
- **4.5 (P3)** As Ana, I want a form-filling helper (detect the form, fill, submit, land).

### Epic 5 — Onboard a dataset with a human in the loop

- **5.1 (P1)** As Dana, I want to write a brief (dataset description, fields, search
  terms) with a template picker, run the pipeline, and watch the stages as a DAG with
  gates (green/red), reviews (flags) and the artefacts each produced, so that the process
  is transparent. *UX:* a horizontal stage rail; each stage expands to its artefacts
  (seeds, crawl map, candidates ranked, evaluation, flags, query + sample rows). *API:*
  *new:* `POST /pipelines/onboard` (server-side LLM), PipelineEvents on the stream,
  `resume`.
- **5.2 (P1)** As Rae, I want the confirm gate as a single decision screen — the chosen
  page's preview, the assessment (queryable? complete? paginated?), sample rows, "yes /
  no / pick another candidate" — so that I can approve without understanding the pipeline.
  *UX:* shareable link to just this screen; a comment field; who decided, when.
- **5.3 (P2)** As Dana, I want to edit the authored query in the Query workspace and
  re-validate, so that a near-miss is fixed by hand, not re-authored.
- **5.4 (P2)** As Dana, I want to onboard N companies for one brief and see a results
  grid (ok / exited / failed with reason, cost), so that batch onboarding is visible.
- **5.5 (P3)** As Dana, I want the reviews (crawl / select / query grades) shown with
  their issues and a "fix" affordance.

### Epic 6 — Runs, schedules, monitoring (P2 epic)

- **6.1 (P2)** As Dana, I want every execution (a query, a crawl, an onboarding) listed
  with status, rows, duration, cost and a trace link, so that nothing is lost. *API:*
  *new:* a runs store.
- **6.2 (P2)** As Dana, I want to schedule a blob (interval / cron), see each run's rows
  and a diff against the previous run, and get notified when rows drop to zero or a
  required field goes empty, so that drift is caught. *(Roadmap M4.)*
- **6.3 (P3)** As Oli, I want runs grouped by session/user with quotas.

### Epic 7 — Traces: see everything, replay anything

- **7.1 (P1)** As Oli, I want a trace timeline that is actually readable: events grouped
  by document and by loop round, collapsible, colour-coded by topic, with a scrubber that
  moves the snapshot / rrweb replay / network list together, so that I read a run like a
  story. *UX:* a time axis with lanes (network, DOM, actions, loop, pipeline, errors); the
  right pane follows the scrubber. *API:* `/traces*` (exists), rrweb per document (*new:
  chunk filter by document*).
- **7.2 (P1)** As Ben, I want the ledger view: every error with its code, remedy, op,
  subject and a jump to the moment in the timeline, so that failures are diagnosable.
- **7.3 (P1)** As Oli, I want the three replay modes as buttons — "inspect offline"
  (static), "re-run from HAR", "re-run live" — with the result compared to the recording
  (rows equal? snapshots equal?), so that drift is measured. *API:* *new:* replay
  endpoints (server-side `Replay`, `WebClient(har=…)`).
- **7.4 (P2)** As Oli, I want live tracing: start a trace on the current session from the
  UI and watch it fill, then persist it, so that debugging needs no code.
- **7.5 (P2)** As Ben, I want a trace shared as a link (or exported as a zip) and opened
  by a colleague, so that support has evidence.
- **7.6 (P2)** As Oli, I want the resource lane (pool waits, quotas, RSS) on the timeline,
  so that slowness has a cause.
- **7.7 (P3)** As Ben, I want two traces diffed (same plan, two days).

### Epic 8 — Tools and code

- **8.1 (P1)** As Ben, I want every tool as a form (from its schema), a "run" button, and
  the result rendered by type (rows → table, markdown → rendered, card → chips), so that I
  can try the API without curl. *API:* `GET /tools`, `POST /tools/{name}`.
- **8.2 (P1)** As Ben, I want the "as code" drawer on every screen: the Python, the HTTP
  call and the MCP tool call that reproduce what the screen just did, so that the
  Playground is a code generator. *UX:* tabs · copy · "open in docs".
- **8.3 (P2)** As Ben, I want an MCP config snippet for Claude Code / Claude Desktop with
  my token, so that wiring an agent is a paste.

### Epic 9 — Settings and operations

- **9.1 (P1)** As Oli, I want sessions listed (id, ttl, pages held, quota) with close /
  extend, so that a stuck session is fixable. *API:* `/sessions` (partial), *new:* list.
- **9.2 (P1)** As Ben, I want scripts listed with enable/disable and the policy, and the
  drivers (resolve / crawl) selectable, so that behaviour is configurable per session.
  *API:* *new:* `/scripts`, `/drivers`.
- **9.3 (P2)** As Oli, I want resolve policies (retry / rate / proxy / antibot / browser)
  edited as forms and applied to a session, so that resiliency is configurable without
  code.
- **9.4 (P2)** As Oli, I want tokens/users (at least: a per-deployment token, per-user
  API keys), so that a team can share one deployment. *(Decision: auth model.)*
- **9.5 (P3)** As Oli, I want limits (pool, quotas, TTLs) read-only with their env names.

### Epic 10 — Cross-cutting

- **10.1 (P1)** Command palette (⌘K): go to workspace, open a recent run/trace, run a
  tool, paste a URL → Explore.
- **10.2 (P1)** The run bar: the live event stream for the current session, filterable,
  pausable, with a "start trace" toggle; a click opens the event in Traces.
- **10.3 (P1)** Empty / loading / error states designed for every screen: an empty
  Explore invites a URL; a 0-row Query explains (ledger hint); an error shows the remedy.
- **10.4 (P1)** Keyboard-first: every action reachable; focus management in the
  preview/table pairs.
- **10.5 (P2)** Shareable state: every screen's state in the URL (workspace + inputs +
  selections), so links reproduce a view.
- **10.6 (P2)** Onboarding tour on first run (three steps) and inline "why" popovers
  on flags, tiers and hints.
- **10.7 (P3)** Multi-tenancy: workspaces per team; saved queries/briefs shared.

## 5. UX principles (the brief for design)

1. **Show the decision, not just the result.** Every automated choice (tier, edge, record,
   candidate) is displayed with the evidence that made it and an affordance to override
   it. The product's honesty is its UX.
2. **Point, don't type selectors.** Users pick records and fields by clicking a preview
   or a numbered table; selectors are generated, durable, and shown only in "as code".
3. **One scrubber.** In a trace, time is one axis; snapshot, DOM replay, network, loop
   rounds and errors follow it together.
4. **Interrupt anywhere.** Auto modes are visibly pausable; a waiting Ask is a decision
   card, never a spinner.
5. **Every screen is a code generator.** Nothing done in the Playground is unrepeatable.
6. **Non-technical screens exist.** The confirm gate (5.2) and the Analyst extract
   (1.1 → 2.1 → download) must be usable with no jargon; advanced panes collapse.
7. **Same components as the website.** The trace timeline, wireframe and preview are one
   component library, rendered read-only on the site.

## 6. Key screens to design first (in order)

1. **Explore** (1.1–1.3) — the entry point; sets the visual language (preview + findings).
2. **Query builder** (2.1–2.3) — the core value; the hardest interaction design (pick
   record/fields on a preview, live rows).
3. **Trace timeline** (7.1–7.3) — the observability signature; the scrubber model.
4. **Confirm gate** (5.2) — the non-technical screen; must be shareable and instant.
5. **Crawl map** (3.1–3.2) — the steering model (auto/manual/locate, Ask cards).
6. **Interact** (4.1–4.2) — live mirror + element table + recorded steps.
7. **Onboard stage rail** (5.1), **Tools** (8.1), **Settings** (9.1–9.2), **Run bar** (10.2).

Deliverables per screen: user flow, low-fi wireframe, hi-fi mock (light/dark), states
(empty/loading/error/waiting), keyboard map, the API contract it needs.

## 7. Backend contract needed (beyond today's service)

| Need | Why | Shape |
|---|---|---|
| server-held **sessions with live pages** | Interact, Crawl steering | `POST /sessions/{id}/pages` · actions over ws · record/replay |
| server-held **crawls** | steer / resume from the UI | `POST /crawls`, `/crawls/{id}/step|resume|state` |
| **pipelines** run server-side | Onboard with the confirm gate | `POST /pipelines/onboard`, `/pipelines/{id}/resume`, events on the stream |
| **runs store** + schedules | Runs, monitoring | `/runs`, `/schedules` (M4) |
| **replay** endpoints | 7.3 | `POST /traces/{id}/replay?mode=static|har|live` |
| rrweb **per document** | 7.1 | `/traces/{id}/rrweb?document_id=` (exists) + chunk boundaries |
| **scripts / drivers / policies** admin | Settings | `/scripts`, `/drivers`, `/policies` |
| **auth** | teams | token → users/keys (decision) |
| **shareable state** | 10.5 | URL-encoded state; server-side saved views |

## 8. Proposed stack (installs are fine)

- **Vite + React + TypeScript**, TanStack Router/Query, **Tailwind** with the shared
  design tokens, **Radix** primitives (accessible dialogs/popovers/command palette),
  **rrweb-player** (vendored, version-matched to the recorder), a small graph library for
  the crawl map (e.g. `d3-force` or `reagraph`), CodeMirror for the "as code" drawer and
  the blob editor, and a WebSocket client for `/events`.
- Served by the FastAPI service at `/playground` (static build) with the new endpoints
  above; developed with the Vite dev server proxied to the service.
- Storybook for the component library shared with the website.

## 9. Open decisions

1. Auth and multi-user model (single token vs users/teams) — shapes Runs, Settings,
   sharing.
2. Which LLM runs server-side for Onboard / the index author (key management).
3. Do server-held live pages (4.1) ship at launch (needs a page-session protocol) or
   after Explore/Query/Traces?
4. Design tooling: Figma files vs code-first design in Storybook.
5. Name of the app (Playground / Studio / Console).
