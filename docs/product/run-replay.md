# Run replay: watching a run (live or recorded) as the plan it is

Status: phases 1–3 built, 2026-09-25. Run is also the trace viewer: the Traces workspace is gone
(`/traces/<id>` redirects to `/run?trace=<id>`).

## The problems (user feedback)

1. **Replays that say nothing.** Half the events show only as a popup card with the event's name.
2. **Fan-outs replay the same item.** The replay finds each step's element with its selector
   alone, so it always highlights the FIRST match. Item 37 looks like item 0.
3. **`select` and `select_all` are not shown.** They are what the run does to a page, and they
   should be highlighted like clicks.
4. **Too many events.** A 200-book run is about 11,000 events; a flat list is useless at that
   scale.
5. **No view of what the run did.** Nothing shows which pages were resolved and when, the
   background requests, or DOM changes against the page.

## The model: every event has a place

Each event is attributed to three things:

- **A STAGE.** The card of the pipeline graph it belongs to. This uses the same per-item,
  per-predecessor attribution as the graph's counts.
- **An ITEM.** The fan-out index path the bus stamps (`[3]`, `[3, 1]`). The root is `[]`.
- **A DOCUMENT.** The page it happened on (`document_id`). Each document has its snapshot:
  static HTML, or an rrweb recording when a browser rendered it.

Everything below is a view of (stage × item × document × time), so nothing needs a flat list.

## The layout (the Run workspace, for live runs and traces alike)

```
┌ run bar: status · ⏮ ◀ timeline ▶ ⏭ · speed · follow ●  · trace ↗ ────────────────────────────┐
├ PIPELINE GRAPH (left, ~60%)                  │ REPLAY SCREEN (right, ~40%)                    │
│  cards with their item cells;                │  the page the selected item is on at t          │
│  click a card = select the stage;            │  (snapshot / rrweb), the step's element         │
│  click a cell = select the item              │  outlined + labelled (the item's OWN element);  │
│                                              │  header: item 37 · /catalogue/… · 200 · 153 ms  │
├ ACTIVITY LANES (Gantt, zoom / pan) ──────────────────────────────────────────────────────────┤
│  one lane per card: a bar per item run (start → end), coloured by action, red on failure      │
│  network lane: every request as a tick (navigation / fetch / resource), retries stacked       │
│  a cursor line at t; drag to scrub; a lane's bars bin at scale (never 10,000 elements)        │
├ EVENTS, grouped: Stage ▸ Item ▸ events (counts; topic filter; one line each; click = seek) ──┤
└────────────────────────────────────────────────────────────────────────────────────────────┘
```

- **Selection.** Selection is a stage plus an item. **Follow** (the default while a run is
  live or playing) moves it to the item that was just active, so the replay screen "moves
  through" the fan-out. Clicking a cell, a bar or an event pins it.
- **The replay screen.** It shows the document of the selected item's current step at t:
  - **Static page.** The snapshot HTML, in a sandboxed frame.
  - **Browser page.** The rrweb recording, seeked to t.
  - **Outline.** The step's element is outlined, with the op as its label: `select h3 a`,
    `attr title`, `click …`.
  - **`select_all`.** Every match is outlined, and the current item's match is emphasised.
- **Nth-item highlighting.** An item's element is the k-th match of its fan-out on that page:
  - **Offset.** Earlier pages' fan-outs are subtracted when the fan-out spans pages (paginate),
    so records 20–39 map to page 2's matches 0–19.
  - **Nesting.** Inside a record, the step's selector runs WITHIN that record's element.
  - **Table rows.** Item `[k, j]` is the j-th row on book k's page.
- **Activity lanes.** They show when each stage ran for each item, and they show parallelism
  directly, eight bars overlapping. The network lane shows fetch and retry timing. Bars bin
  when there are more than fit.
- **Grouped events.** The event list is a tree:
  - **Levels.** Stage, then item (collapsed, with counts and state), then the item's events.
  - **Lazy rendering.** Only expanded groups render.
  - **Readable rows.** Events are one line each, wrapped, never wider than the column.
  - **Other events.** Events with no stage go under "page & network" and "other".
- **Popups.** No popup cards. What the old pulses said is on the lanes and the graph; the
  screen only outlines elements and moves the pointer.

## Sources

- **Live run.** `GET /runs/{id}` gives the events. Their `n` is the bus sequence, which is also
  the trace's.
- **Recorded run.** A trace opens in Run with `?trace=<id>`: its events, and its plan from
  `/traces/{id}/plan`.
- **Page contents.** They come from the trace's snapshot events (`/traces/{id}/events/{n}`).
  They are fetched lazily, only for the page on screen.

## Phases

1. **Core replay.** Open a trace in Run, the attribution model, grouped events, the replay
   screen with nth-item highlighting, the activity lanes, and "Replay in Run" from Traces.
2. **Browser pages.** rrweb seeking per document, with DOM changes marked on the lanes.
3. **Other traces.** Loops and crawls on the same model: the loop's rounds as the stages.

## Built (phase 1)

- **Trace mode.** `/run?trace=<id>` opens any recorded run with the graph, lanes, replay screen,
  rows and grouped events. Row events now carry the row, so rows replay; older traces say they
  have none. Traces has a **Replay in Run ▸** button on plan traces.
- **Replay screen.** It shows the item's page from the trace's snapshots
  (`GET /traces/{id}/documents/{doc}`, parsed traces cached). A record's step is placed on the
  page whose span of the fan-out holds its index, and outlined within that record: item 7's
  title read outlines the 8th book. The frame scrolls to the target itself and keeps it in view
  as the page's images load, never scrolling the app.
- **Activity lanes.** Per-pixel concurrency per stage, run-length drawn, so a 200-item fan-out is
  a band, plus the network lane. Click or drag to scrub.
- **Events tree.** Stage ▸ item ▸ event, with only opened groups rendered, and errors on top.
- **Playback.** Play at 1–64×. **Follow** tracks the active item; a click pins a stage or item.
- **Traces view.** No event cards (the Player's `pulses={false}`).

## Built (phases 2–3), after the first review

- **Playback.** Step by step is the default: one visible moment every 0.4s (a select, a read, an
  action, a page fetched). A run's steps are milliseconds apart, so no time speed showed them. The
  recorded clock is available at 0.1×–16×.
- **Browser pages.** A page with an rrweb recording replays that recording, seeked to the moment,
  with the step's or action's element outlined on the rebuilt DOM and scrolled into view. Other
  pages show their snapshot AT that moment (`upto` the latest snapshot before it), not the final
  page. Actions (click, write) are outlined like selects.
- **Other traces.** Mark lanes show each loop's rounds (waiting for a person in amber), each
  pipeline's stages (enter → exit bars, red when stopped), background requests, scripts, DOM
  changes and errors. A trace with no plan shows a PROCESS panel in place of the graph: the
  pipelines' stages in order (duration, passed or stopped), the loops (rounds, last decision), and
  the pages opened (click one to replay it). The Traces list opens every trace in Run.
- **Clean traces.** A run's trace starts at the run (`since=bus.cursor`); it used to replay the
  bus's retained history, so earlier runs appeared in it. Concurrent runs on one engine still share
  the bus; isolating them per run is open.

## Built (after the second review)

- **One workspace.** With nothing loaded, Run lists the recorded traces with their size in KB, and
  can delete them or clear all of them (the site's own traces are kept). The run bar's **recorded ▾**
  opens any of them.
- **Compact screen.** Activity, Output and Events start folded, and the lanes are 9px each.
- **The element, clearly.** The item's own element is SPOTLIT: the rest of the page is dimmed, the
  element is outlined, and it is centred on screen. The centring is re-checked as images load.
- **The i-th iteration everywhere.** Older traces have no item paths and no fan-out events. For them,
  the iteration is counted from the fan's own step, and the fan feeding each stage comes from the
  plan's structure. Item 37 of a two-page `select_all` spotlights the 18th book on page 2.
