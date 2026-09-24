# The Author workspace — Explore + Query + Interact as one flow

*Status: proposed 2026-09-25, for review before building. Replaces the three separate
Playground workspaces with ONE that carries a person from a URL to a tested, runnable scrape
without changing screens. Wireframes are in Storybook (`Wireframes/Author`).*

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
