# Run: a plan, played

Status: built, 2026-09-25 (it replaces the old run-replay design). The package contract and the UI's layers below exist and are tested.

## The one idea

**Everything is a plan.** A run is a plan plus the stream of events it produced.

- **Executed plans.** When a plan runs, every event is stamped with the address of the step that caused it.
- **Imperative calls.** Calls made under `wc.record()` become the recording's plan, and their events are stamped with the step each call became.
- **Traces.** A trace carries its plan in its footer.

So the Run view never guesses. It joins events to plan nodes by `(step, item)`:

```
RUN(t) = fold(PLAN, events[0..t])
```

- **The plan view** is `RUN(0)`: nothing has happened yet.
- **A live run** is `RUN(now)`, and the cursor follows the end.
- **A replay** is `RUN(t)` for a scrubbed `t`.

These are one component at different moments, not three screens. "Plan play" and "event replay" are the same thing.

## What the package guarantees (the contract)

Every event carries the following:

| field | meaning |
|---|---|
| `step` | The address in the plan, e.g. `"4/kw:detail/4"`. A path segment is either a step index (the `get` of a get+call pair), or the arg a sub-plan sits in (`kw:<name>`, `arg:<n>`, and `sub` for a bound op's own sub-plan). |
| `item` | The fan-out index path, e.g. `[7]` or `[7, 2]`. |
| `document_id` | The page the event concerns. |
| `run_id` | The run the event belongs to. Concurrent runs never mix. |
| `plan_id` | The plan whose run it is (`Plan.id`, a hash of its root, source and steps). A trace can hold several plans' runs; a sub-plan's events carry its plan's id. The trace's `/plan` and `POST /runs` return it. |
| `ts` | Its time. |
| `n` | Its order. |

Every step publishes two events:

- **`step`** when it starts: the op, its selector and its args.
- **`result`** when it ends:
  - `kind`: Reference, Document, Element, Collection, Field, list or value.
  - `document_id`: the page. For an Element, this is the page it is on.
  - `n` for a collection, or `preview` for a value.
  - `ms`.
  - `ok`, plus `error` and `message` when it failed.

Everything a step causes carries its address:
- network requests, navigations and snapshots;
- rrweb DOM chunks;
- actions;
- errors;
- the fan-out's `parallel` and `fanout` events.

The recorder publishes the same `step` and `result` events for recorded calls. A recorded session's trace therefore replays against its plan exactly like an executed one.

## The model (UI, pure, tested)

**`planModel(plan)`**: every step of the plan and its sub-plans, by address. Each node has:
- its op, arg and kwargs;
- the object type it yields (Reference → Document → Element / Collection → Field; the same `typeAfter` rules the Author graph uses);
- its parent object;
- the column it produces;
- whether it fans out;
- the fan-out that feeds it.

**`reduce(state, event)`**: the run state, built one event at a time. This is the same code for a live stream and a recorded one.

- **`nodes[addr]`**
  - Instances by item: state (pending, running, done, failed), start and end, result, error.
  - Totals: done, failed, running and expected (from the fan-out's `n`).
- **`docs[id]`**
  - The page's url, status and tier.
  - The step and item that opened it.
  - Its snapshots and rrweb chunks (event indexes).
  - Its requests, actions and errors.
- **`requests`**: every network event, with its document, step, item and timing.
- **`rows`**: the projected rows, with the item each came from.
- **`errors`**: each with its step, item and document.

**`RunFolder`** holds the run at a cursor and folds forward incrementally (a live stream, playback); a step back refolds from the start once. **`locate(model, state, step, item)`** gives the item's page and the way down to its element (`ol.row li[7] › h3 a`) from the step results. **`tell(event)`** says an event in words.

## The screen

```
┌ bar: source (run / trace / plan) · ▶ ❚❚ · step ◀ ▶ · speed · t ─────────────── live ● ┐
├ RUN GRAPH (the plan, materialising) ──────────────────┬ PAGE (the selected item's) ──────┤
│ Reference ─resolve→ Document ─select_all→ Collection  │ the Document at t: a snapshot, or  │
│   url · 200 · 120 ms        ▣▣▣▣▣▣▣▣▢▢▢▢ 12/40 ×8     │ the rrweb recording at t; the      │
│                             │ each element (item k)   │ step's element spotlit; the        │
│                             ├ title: select→attr ─ "Sapiens"  pointer on actions; the     │
│                             ├ detail: …resolve→ Document (its own page) → description      │
│                             └ price: select→attr ─ "£54.23"   │ current event as a card    │
│  project ⇒ TABLE  title  price  detail   (arrows from each Field)   │ ── its requests ── │
├ TIMELINE: one time axis ─────────────────────────────────────────────────────────────────────┤
│ steps  ▂▃▅▇▇▇▅▃   (a lane per plan node: bars = items running; height = parallelism)        │
│ pages  ▮  ▮▮▮▮▮▮  (navigations, one per document)                                           │
│ net    ···|··|·   (background requests: xhr / fetch / assets, by document)                  │
│ act    ◆   ◆      dom ░░▒▒░   errors ✕                                   cursor ▏ scrub     │
├ ROWS (projected so far; click a row = its item) │ EVENTS (the selection's, in plain words) ──┤
```

- **Show, then tell.**
  - Every change is seen first: a node materialises, a cell fills, a dot travels along an edge, a page flips, an element lights up.
  - Every change is also said in one line: "item 7 · title read → 'Sapiens'". The same line appears on the page card, in the events list, and in each node's tooltip.
- **Objects are nodes; ops are edges.** This is the Author graph's grammar.
  - A **Document** is a page card: host, path, status, tier, and its actions as badges.
  - An **Element** or **Collection** is a strip of cells, one per item. From about 80 items it becomes a density bar with counts, and hovering shows the item.
  - A **Field** is a value chip showing the selected item's value.
  - **Attributes come out of** the Document or Element they are read from.
  - **Project** is a table node, with an arrow into each column from the Field that fills it.
- **The fan-out.**
  - The Collection node's cells are the items.
  - A nested fan-out (a detail page's table rows) shows as cells inside the parent item's row when that item is selected.
  - Parallelism is the number of cells lit at once, and "×8 at once" is its tell.
- **The selection** is `(node, item)`.
  - **Follow** (the default while live or playing) selects the oldest item in flight, so the page and chips move item by item and never flicker between parallel items.
  - Clicking a cell, row, bar or event pins the selection.
- **Errors** turn the step's edge red where they arose, with a count. Clicking one selects its item and moves to its moment.
- **Animation.** Positions come from a deterministic layout: the plan never moves as it runs, so nothing jumps.
  - Nodes grow in place, cells fill, and edges pulse; all are CSS transitions of 150–250 ms.
  - Scrubbing jumps without animation.

## Layers (UI)

1. **`lib/run/plan.ts`**: `planModel`, addresses and types.
2. **`lib/run/state.ts`**, **`locate.ts`**, **`tell.ts`**: `reduce`, `RunFolder`, `followItem`, `locate`, `tell`.
3. **`lib/run/layout.ts`**: a fixed layout of plan nodes (columns by depth, rows by branch).
4. **Components**, each small and stateless, each with a story:
   - `RunGraph`, built from `StepCard` and `ItemStrip` (stories: `Run/A plan, played`);
   - `RunTimeline`;
   - in the Playground: `PageStage` (the existing `PageFrame` / `Player`, plus an event card) and `EventFeed`.
5. **The Run scene**, which only wires things together: the source (a live run, a trace, or a plan alone), the cursor and play, and the selection.

## Nested crawls: the chain of pages

When a link read off a page is opened (a `resolve` of an `attr("href")`), up to n deep, the page area shows the item's **chain of pages** (`runLib.pageChain`), so it never swaps back and forth:

- **The page it opened** is the main pane. It shows the step on screen, spotlit, with the event card and the page's requests.
- **The page it came from** sits beside it, smaller, with **the link it followed** spotlit.
- **An arrow** is drawn from that link to the page it opened, labelled with the step that opened it (`resolve()`). It draws itself in when the item moves on to a new link.
- **Pages further back** fold into compact chips ("page 1 · ↓ ol.row li › h3 a"). There are never more than two panes.
- **Space.** While a chain is open, the page area takes the wider share of the screen (the graph narrows).

Frames and recordings report where their spotlight is on screen (`onSpot`), which is where the arrow starts. A `limit(n)` keeps its items' positions, so steps after `select_all(...).limit(n)` still run per item of the `select_all`, and their counts are capped at n.

## Recorded scripts are plans too

Under `wc.record()`, what a script READS off the page it reached is recorded as well: `select`, `select_all`, `attr`, `text_content`, `links`, and a `resolve` of a link read off it. They form a tree over the journey's page.

- **Loops.** A collection's items share one "each item" node, so a loop over the cards records its reads once. Each read is stamped with its item.
- **`rec.reads_plan`.** It compiles the tree to the plan that does the same in one go: the journey, then `.select_all(…).extract(<a column per read>).project()`. Running it gives the same values the loop read.
- **Events.** Every read runs as its step. Its events are stamped `@n<k>` with the item, and publish `step` and `result` like a plan's steps.
- **The trace.** A trace of the session carries the compiled plan and a map from each `@n<k>` to its address in it (`/traces/{id}/plan` returns `steps`). Run folds a recorded script exactly like an executed plan.
- **`rec.plan`** is still the journey alone, replayable to the page it reached.

A step's result says what a collection holds (`of`): a `select_all` gives a list of **Elements**, whether run eagerly or as a plan. The graph labels it `ELEMENT[] · N`.

## Next

- Reads recorded before an interaction replay after it. The reads hang off the journey's last page.
- A scoped name for elements, so errors on elements can name them.
