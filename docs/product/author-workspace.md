# The Author workspace — Explore + Query + Interact as one flow

*Status: built. Proposed 2026-09-25 with modes (look / pick / drive), reworked the same day
into the plan-centric, scope-based workspace of §8 -- no modes, one menu generated from the
object's surface. Sections 1-7 are the original proposal and its review; §8 is what stands.
Wireframes are in Storybook (`Wireframes/Author`).*

## 1. Why merge

Today a scrape is authored across three tabs: Explore (what is this page), Query (which
rows and fields), Interact (click / type / scroll on a live page). Real scrapes need all
three at once: open the listing, log in or dismiss a cookie wall, pick the record, follow
the pagination, click into a detail page, pick more fields there, then run the whole thing.
Every hand-off between tabs loses the thread. The Author workspace keeps one page, one
session document, one plan, and lets the person switch *what they are doing* (looking,
picking, driving) without switching *where they are*.

## 2. Personas and the journeys that must work

| journey | today | in Author |
|---|---|---|
| **J1 static listing → rows** (Dana) | Explore → Query → run | paste URL, the record is suggested, click two fields, rows appear, Run |
| **J2 JS page → rows** (Ben) | Explore (spa flagged) → re-explore browser → Query | auto tier escalates; the same page, now rendered; pick as before |
| **J3 behind a login / cookie wall** (Ben, Oli) | Interact → log in → copy URL → Query | switch to *drive*: type into the form, click; the page changes in place; switch to *pick*; the plan records the steps |
| **J4 listing → detail pages** (Dana) | not possible | pick the record, pick the link field, "follow into each": a detail page opens as the *next stage*; pick fields there; rows are nested per record |
| **J5 paginated** (Dana) | Query pagination select | the pagination flag offers "follow rel=next"; the plan shows the paginate step; Run walks the pages |
| **J6 infinite scroll / load more** (Dana) | Interact scroll, no rows | *drive*: scroll or click "load more" until enough; *pick*; the plan records the scroll loop |
| **J7 author for a model** (Ben) | Tools | the same plan as blob / Python / MCP, and "explain" — one click away, always |
| **J8 compare / verify** (Dana) | none | the local preview rows vs the server run; a diff count; re-run any time |
| **J9 resume tomorrow** (all) | lost | the workspace state is a plan blob in the URL / a saved draft; reopening restores the page (a fresh capture) and the picks |

## 3. The one screen

Three regions, fixed; only their contents change.

```
┌──────────────────────────────────────────────────────────────────────────────────────┐
│ loaded:  ● case-studies (static)  ● cost (live)  ○ product/1 …          pages 3/4 free  │  ← the session's documents (exists)
├──────────────────────────────────────────────────────────────────────────────────────┤
│ [ URL ........................................ ] [auto ▾] [Open]   ● live  ⟳ reload   │  ← the address bar (one)
│ mode:  (look)  (pick)  (drive)          record: article.row ×4 · .row .rounded-lg .p-4 │  ← what am I doing + what is selected
├───────────────────────────────────────────────┬──────────────────────────────────────┤
│                                               │ STAGES                               │
│                                               │ ① listing  case-studies   ✓ 4 rows   │  ← the plan as stages (pages)
│         THE PAGE (Player)                     │ ② detail   ↳ a.name  (each)  · 2 fields│
│   highlights follow the mode:                 │ + add a stage (follow a link / paginate│
│   look  → flags, record list, controls        │   / drive: click, write, scroll, wait) │
│   pick  → record + fields, hover readout      ├──────────────────────────────────────┤
│   drive → the target, the pointer, the live   │ THIS STAGE                           │
│           DOM stream                          │ record  [article.row      ] ×4  detected│
│                                               │ fields  [a.name ✓][time ✓][.sector]…  │
│                                               │ steps   click #load · scroll · wait   │
│                                               ├──────────────────────────────────────┤
│                                               │ ROWS  local 4 · server —  [Run ▶]     │
│                                               │ ┌ name        │ when     │ detail ▸ ┐ │
│                                               │ │ Price mon…  │ 2026-09… │ {3}      │ │
│                                               │ └────────────────────────────────────┘ │
│                                               │ Plan · As code · Flags · Skeleton      │  ← the drawers
├───────────────────────────────────────────────┴──────────────────────────────────────┤
│ ▶ ◼ ⏮ ⏭  0:02.3 / 0:14.0  ──●───────── beat 0.9s  1×   (only in drive / replay)     │  ← the media bar (exists)
└──────────────────────────────────────────────────────────────────────────────────────┘
```

- **Modes** are a radio: *look* (Explore's job: flags with evidence, the record list, the
  controls, the skeleton), *pick* (Query's job: record, fields, class chips, local rows),
  *drive* (Interact's job: click / write / scroll / wait / goto on the live page, the pointer
  moving, the DOM stream). Switching mode never reloads the page.
- **A stage** is one page of the plan: the listing, a detail page reached by following a
  field, a page reached by driving (goto / click). Stages are the plan's `.step()` /
  `.follow()` / `.paginate()` structure made visible. The active stage's page is what the
  Player shows; clicking another stage shows that page (its capture, or the live page).
- **Rows** are always the local preview of the active stage, with nested cells for a
  followed stage (`detail ▸ {3}`), and the server run beside them once run.
- **The drawers** (Plan / As code / Flags / Skeleton / Elements / Events) are tabs in the
  bottom of the right column — never a separate screen.

## 4. Stories (numbered for the wireframes)

- **A1** As Dana I paste a URL and the page opens with the *auto* tier into my session; the
  tier select is always visible and changing it re-opens the page with that tier.
- **A2** As Dana in *look* mode I see the flags (with evidence), the detected record list
  outlined, and the controls; the hover readout names the element and its classes on ONE
  line that never moves the page.
- **A3** As Dana in *pick* mode I click a repeating element and the record is chosen (the
  smallest repeating ancestor); its classes are chips and the one the selector uses is lit;
  clicking another chip switches the selector.
- **A4** As Dana I click fields inside a record (or their chips); each column shows its
  selector with the used class lit, a sample, and the match count; rows fill locally.
- **A5** As Dana I pick a link field and choose *follow into each*: a second stage opens
  (the first record's detail page, fetched into the session); I pick fields there; the
  listing's rows gain a nested cell per record.
- **A6** As Ben in *drive* mode I click a control in the page (or pick it and choose an
  action): the plan records the step, the live DOM changes in the Player with the pointer
  travelling, and a later *pick* reads the new DOM.
- **A7** As Ben the `pagination` flag offers "follow rel=next (N pages)"; the plan shows the
  paginate step; the server run walks the pages; the local preview stays the one page.
- **A8** As Dana I press Run: the plan executes through the session; the server rows appear
  beside the local ones with a match / mismatch count; every step's events pulse in the
  Player (a replay of the run I just made).
- **A9** As Ben the Plan drawer shows the explain tree and the wireframe; As code shows the
  blob, Python, HTTP and MCP; copy in one click.
- **A10** As anyone, back / forward and reopening the URL restore the stages and picks (the
  plan is in the URL as a blob); a lost session re-opens the pages as fresh captures.
- **A11** As Oli the loaded strip shows what the session holds and lets me release pages;
  a page held live is marked; closing a stage releases its page.
- **A12** As Dana a wrong selector is a designed 0-row state naming the nearest record
  list, never an empty grid.

## 5. What the merge removes

- The Explore, Query and Interact tabs (Home links point at Author with a mode).
- The two ways of opening a page (tool call vs session): only the session.
- The separate "Rows here" in Interact; the DataFrame is always the active stage's rows.

## 6. Backend needs (small)

- `POST /sessions/{sid}/documents` already opens; a *follow* is an open of the first
  record's link (a capture) tagged with its parent stage (client-side).
- The plan builder emits `.follow(field)` — check `Expr` has a `follow` / `documents(column)`
  step (it has `documents(column)`); nested rows come from `extract(detail=<sub-plan on the
  followed document>)`.
- Drive steps in the plan are the recorder's `.step(doc.click(...))` — the session's
  `record()` could mirror the live actions automatically; else the UI appends them.

## 7. Open questions for review

1. Should *drive* actions be recorded into the plan automatically (a recording session)
   or only when the person says "add as step"? (Proposed: automatically, with an undo.)
2. Nested rows for a followed stage: one sub-object per record (proposed) or a flat join?
3. Is a stage list enough, or do you want the stage graph drawn (listing → detail → …)?
4. Live pages per stage cost a pool page each; proposed: only the active drive stage is
   live, the others are captures.


## 8. What was built (2026-09-24): the plan is the model, the scope is the last object

The review of the first build asked for three things: no modes ("I just want to hover over
an element and click it"), a menu that is *the object's interface* rather than a hand-picked
list, and a better way to choose the classes that make a selector. The workspace now works
like this.

**The plan is the state.** `Author.tsx` holds `{url, tier, plan}` where `plan` is the plan
IR exactly as `/plan` and `/execute` take it (`packages/ui/src/lib/plan.ts` addresses calls by
a PATH into nested sub-plans: `[callIdx, "kw:<field>", callIdx, …]`). The URL carries it
(`?p=`); Save / Export / Import keep it; Run executes it through the session. The plan tree
(`PlanView`) renders live and is edited in place: selectors, attr regexes, typed text,
optional toggles, reorder, remove, rename / remove fields, the pagination node's mode and
bounds. The same component renders a trace's plan and the website's plan demo (the Python
HTML wireframe is no longer used by the UI).

**Click an element → the selector, then the ops.** `ElementMenu` opens at the click. Its top
is the SELECTOR BUILDER: the clicked element's tag, id and classes as toggles, its ancestors
(up to the scope) likewise, a free-text override, and the live match count *inside the
scope* (`×4 in each article.row · 24 on the page`). `li` becomes `li.card` with one click;
a selector that stops matching the clicked element says so. Below it are the object's ops,
GENERATED from `GET /ops` -- the op catalogue the service builds from the cores' backing
tables (the same source as the typed surface; `service.op_catalogue`). Ops that take a
selector (`select_all`, `select`, `click`, `write`, `scroll`, `wait_for`) are buttons, with
inputs for their other required params; the browser actions run on the live page and are
recorded into the plan unless unticked. `attr` is offered as reads (text · href · src ·
count · a named attribute) that become a FIELD; a link offers "open the link ▸" (a new
document joined under the same plan: `select(sel).attr("href").resolve()`); a pager-looking
element offers `paginate` with the right kwargs; every other op of the surface sits behind
"more".

**The scope is the last object.** Every op you take makes its result the scope: after
`select_all("article.row")` the scope is *each article.row* -- the next click builds a
selector relative to the record and an op on it becomes a field of the record's `extract`
(`select(sel).attr(text)`; a nested `select_all` is a list field; an opened link is a
per-row detail page). After a `select` the scope is *that element*; after a link's resolve
it is *that page* (opened into the session, its chain continues there). Esc, the scope
chip, or clicking the plan's root sets the scope back to the page; clicking any plan node
makes it the scope, so you can go back to the record, or to a followed page, at will. A
page-level op (an action, the pager) always goes into the page's chain after its resolve
and the actions there, before the data ops.

**Pages.** Each `resolve` in the plan owns a page in the session, keyed by its path; the
root's is the URL, a followed one's is the href its chain reads off the page before it
(evaluated locally on the rebuilt DOM). Only one page is held live at a time.

**The page panel** shows what the client found: the card (kind · status · tiers · title ·
timing), the signal flags with evidence, and the detected pattern groups (each its own
colour, outlined on the page on request). Tabs: Rows (local preview) · Server run · Skeleton
· Markdown · Elements · As code. The event feed at the bottom of the Playground is off by
default (an `events` chip in the header turns it on for debugging).

**Pagination** (`paginate`): `by="link"` follows `rel=next` (or `next=<selector>` for a site
without it), `by="param"` walks `?page=`, `by="cursor"` carries a token, and `by="click"`
drives an interacted pager on the live page (click a load-more control, or scroll to the
bottom, until `records` stops growing); the plan node edits all of it.
