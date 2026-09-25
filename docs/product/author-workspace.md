# The Author workspace — Explore + Query + Interact as one flow

*Status: built as the GRAPH BUILDER, driven as a focusable literal plan (§10-§11). Proposed 2026-09-25 with modes (look / pick / drive), reworked the same day
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


## 9. The second review (2026-09-24, late): a card, per-op tabs, groups, names from the page

What the user asked for after using §8, and what was built.

- **Every match of the selector under construction is outlined** on the page as you toggle
  it (a dashed "each" / "match" outline), so `li` versus `li.card` is visible before you commit.
- **Candidates by the group they enumerate.** The card offers ONE selector per enumerable
  group the clicked element sits in -- `ol.row li ×20`, `li ×75` -- the best-reading form for
  each (a semantic class on the element, an id, a class-bearing ancestor, else the tag), the
  detected record group first, then smallest first. For `select` the unique forms come
  first (`#product_description ~ p`, `div > p:nth-of-type(2)`), then the groups.
  Utility classes stay available as toggles but never make the default.
- **The selection is a card, not a tooltip** (`SelectionCard`, top of the right column):
  what was clicked, the candidates, the selector text and its count, then one TAB PER OP
  from `GET /ops`. `select_all` shows the count and the fields the group shares (the
  descendants present in most of the first records, wrappers like `h3 > a` unwrapped,
  with the attribute worth reading -- href, title, text, src, alt, datetime, or a class that
  codes a value -- a sample and the coverage), tick to extract. `select` lists everything
  readable off the element (text, child count, every attribute), tick and name the column,
  or NAME IT FROM THE PAGE: a selector whose text is the column's name. The browser actions
  take their params; a link opens as a new document; the pager offers the modes; `more` has
  the rest of the surface.
- **Column names as expressions** (a package change): `.alias(name)` names the value a chain
  yields when it is a positional `extract` column -- a literal, or an expression read off
  the element -- and `merge()` folds a collection's rows into one dict. A key/value table is
  `select_all("tr").extract(doc.select("td").attr("text").alias(doc.select("th").attr("text"))).merge()`.
  The plan tree renders such a column as "name from the page" with the chain under it.
- **Esc** clears the selection first, then returns the scope to the page you are on (a
  followed page stays), then to the root page.
- **Fetches are cached**: opening a page a session already holds at the same tier hands the
  same document back (`reused` in the handle); ⟳ reloads it. The wheel over the page scrolls
  only the page inside (the workspace no longer scrolls with it).

### The user stories this makes possible (the books story)

`books.toscrape.com`: open the catalogue; click a book's `li`; the card offers `li.col-xs-6
×20` (the record) and `li ×75`; tick the suggested fields (the title from `h3 > a`'s
`title`, the price, the rating's class, the cover's `src`); `select_all` -- 20 rows appear.
Click a title link, "open the link": the first book's page opens, joined under the plan as
a per-record field. On it, click the description paragraph, `select` + text named
`description`; click a table row, `select_all` (`table tr ×7`); click a `td`, `select` +
text, name from the page: `th` -- the table is a dict per book. Back on the catalogue
(click the record node, Esc), click "next", `pages` -- the next link is followed. Set
`max_pages`, Run: every book on every page with its description and details. Then edit the
plan in place: add a `filter`, make a field optional, reorder, change a selector.

The same moves cover: a listing whose records nest a list (a `select_all` inside a record
is a list column); a detail page that itself has records (a `select_all` on the followed
page nests rows); a form filled then submitted before the records exist (`write`, `click`,
`wait_for`, recorded at the page level); a load-more pager (`paginate(by="click")`); a
definition list or a table of properties (`alias` + `merge`).

Offline, the same plan is `tests/test_books_story.py` in the package.


## 10. The graph builder (2026-09-25): the package's objects as nodes

§8-§9 kept the plan's linear chain as the model and fought it: a second data op on a page
chained after the first instead of beside it, and a clicked cell became the record. The user's
direction: *"our builder plan is just nodes representing the underlying objects -- reference,
document, collection -- selecting one opens the static / live view of the page; from a reference
the paths are to resolve or read an attribute; from a document we interact or select (it is just
the surface exposed); from the whole chain we have a graph and then project our fields out."*

**The model** (`webclient-ui/packages/ui/src/lib/graph.ts`, tested in `graph.test.ts`): a tree
rooted at the Reference of the start URL. Each node is an object -- Reference, Document (a page,
static or live), Element (a `select`), Collection (a `select_all` / `links`: its children run
on EACH element), Value (text, a count) -- and carries the op that made it from its parent. Its
type comes from the package: `GET /ops` gives every op's `returns` and a Collection surface.
Pages carry their pager and collections their limit as modifiers. A node marked as an OUTPUT
(a name, or a name read off the page = `.alias(expr)`) is a column.

**Compile** (graph → the plan the service runs): the spine is the chain down to where the
outputs branch (open → actions → pages → the records); an Element's outputs flatten into its
parent's columns (a select is cheap to repeat); a crossed page (a `resolve`) or a Collection
becomes ONE nested column (a dict / a list of rows, `merge` when every column is named from the
page) so the page is fetched once. **Decompile** (plan → graph) makes an imported, saved or
traced plan editable in the builder; the two round-trip.

**The screen**: the graph on the left (typed nodes, samples on the page, the op's argument
editable in place, + output, ×); the selected node's view in the centre (a Reference: its URL
and "open it" static / auto / browser / live; anything under a page: that page with the node's
matches and the outputs outlined); on the right the ELEMENT INSPECTOR when you click the page --
its parents with class toggles (↑ re-targets a parent), candidates per group (`li.col-xs-6
×20`, `↑1 table tr ×7`) and unique forms (`#product_description ~ p`), every match outlined,
every readable attribute (text, own text, count, its label, href, src, data-*, aria-*, class)
to tick and name or name from the page, the fields a group shares, and the ops (select_all,
select, click, type, scroll, wait for, open the link, pages) -- which add nodes with the ticked
reads as their children. Otherwise the node's own card (a page's ops and pager; a collection's
shared fields and limit; an element's attributes; a value's samples). Below: Rows (local
preview, detail pages included once opened) · Server run · Plan · Page (card, signals, pattern
groups) · Skeleton · Markdown · Elements · As code.

**Verified**: the books story through the UI against books.toscrape.com (headless) compiles to
`resolve().paginate(next="li.next a", max_pages=2).select_all("li.col-xs-6").extract(rating,
title, price, detail=select("h3 a").attr("href").resolve().extract(description,
info=select_all("table tr").extract(td.alias(th)).merge()).project()).project()` and the server
returns 40 rows with their details; offline, the same plan is the package's
`tests/test_books_story.py`.


## 11. The plan of objects, focused (2026-09-25)

The user kept the object graph as the model ("we are leaning into the models -- references,
documents, collections -- that IS the plan") and changed how it is driven:

- **The plan on the left is the graph written as a literal plan**: `Reference("…")`, then
  `.resolve(browser="auto")`, `.select_all("li.col-xs-6")`, `.select("h3 a")`,
  `.attr("title") → title`, indented by what hangs off what, each line typed (Reference /
  Document / Element / Collection / Value) with its sample. Arguments edit in place.
- **Clicking a line sets the FOCUS**: the page renders only that object (its ancestors keep
  their styling; everything else is hidden) and every new selector is rooted there. A
  collection renders one record at a time (‹ record 3/20 ›). When the focused op still needs
  its own selector, the page renders the op's INPUT instead, so the selector is picked where
  it will be evaluated.
- **The focused line shows its edges**: the ops of its object's surface (`.select_all("…")`,
  `.select("…")`, `.attr("text")`, `.attr("href").resolve()`, `.click("…")`, `.paginate(…)`,
  the page's own ops). An edge that needs a selector is added empty ("⇧click the page") and
  waits: shift-click the page, or pick a SUGGESTION -- records for `select_all` (the detected
  groups and the repeating classes inside the focus), field elements for `select`, useful
  attributes for `attr`, controls for `click` / `write`.
- **The page is a real render**: a static capture renders in a sandboxed frame with its own CSS
  and JS (`PageFrame`; the page never gets the workspace's origin; a small agent inside it draws
  the outlines, isolates the focus, reports shift-clicks and link clicks); a live page is the
  mirrored browser where plain clicks go through to the real page (and can be recorded as
  `.click()` nodes). Hover outlines everything; SHIFT-click picks.
- **Shift-click opens the selector editor** (the inspector): the element's hierarchy up to the
  focus with tag / id / class toggles, candidates by group (incl. the parents' groups: a cell's
  row), every match outlined as you edit, the element's attributes to read (named, or named from
  the page), and "use for .select_all()" when the focused op is waiting for its selector.

Verified with the books story through this flow (headless, books.toscrape.com): the plan builds
line by line with focus rendering the record, the book page and the table row in turn, and Run
returns 40 rows over 2 pages with each book's description and details.


## 12. Picking by click, recording what you do (2026-09-25)

The next review, and what changed:

- **No shift-click picking.** An edge that needs an argument (`.select_all("…")`, `.select("…")`,
  `.click("…")`…) puts the page in PICK mode (the frame is ringed amber): a plain click picks,
  or a suggestion does. Otherwise the page is INTERACTIVE and what you do is RECORDED: following
  a link adds `.select(a).attr("href").resolve()` and opens that page as the focus; clicking a
  control adds `.click(sel)`; typing adds `.write(sel, text)` (typing into the same field again
  updates it); submitting records the submit button's click. Actions need a browser to replay,
  so the page's `resolve` switches to `browser=True`. Hold SHIFT to do any of it without
  recording (a shift-followed link browses away; "back to the page" returns).
- **A collection in focus shows ALL its elements** (the frame keeps every match and its
  ancestors, hides the rest); a click inside any of them roots the selector at that one.
- **Rows** explode nested values into dotted columns (`detail.info.UPC`); on a page opened from
  a record, only the rows whose page is open are shown, with the parent row's columns repeated.
  The server run shows the nested JSON (a JSON toggle).
- **Key → value tables**: a collection whose elements hold two cells (th/td, dt/dd, or two
  children) offers `.extract(td.alias(th))` -- the rows become one dict (`alias` + `merge`); any
  value's column can be named literally or from the page (a selector relative to the record).
- **Files**: `Document.download()` returns the raw bytes as a file value (`url, filename,
  content_type, size, sha256, base64`; the name from `Content-Disposition` when given) --
  `select("a.pdf").attr("href").resolve().download()`; a binary page offers it, and a file cell
  renders as a download link. `.html()` returns a page's HTML.
- **Fixed**: the server run's 500 -- a link read (`attr("href")`) that was itself an output with a
  page opened under it compiled to `extract` on a Reference; a Reference / Value now keeps its own
  column and the page nests beside it, and an op an object lacks is a catalogued 422
  (`op.unsupported`), not a 500. The frame's "rendering…" hang after load (the ready handshake is
  now a ping).


### 12.1 Jump, shift to record, names from a column (2026-09-25)

- An action on the page that the plan already has JUMPS there instead of adding a node: a link
  whose element a `select(…).attr("href").resolve()` already covers focuses that page node and
  opens the page you clicked (another book, the same plan node); a click / typing whose selector
  matches an existing `.click` / `.write` focuses it.
- SHIFT-click RECORDS (the reverse of before): a plain click just interacts (an unmatched link
  browses away, unrecorded); shift on a link records `select(a).attr("href").resolve()` and opens
  it; shift on a control records `.click(sel)`; typing into a field you shift-clicked records
  `.write(sel, text)`. The live page: shift-click records.
- `alias` accepts a column extracted beside it: `select_all("tr").extract(name=th.attr("text"),
  value=td.attr("text").alias(field("name"))).merge()` -- the `name` column is spent as the key
  (dropped from the row); a named column may carry `.alias` (the alias wins). The key/value edge
  builds exactly this.
- Selectors never contain `tbody`: the browser inserts it into every table, the server's HTML has
  none (`tbody tr` matched nothing on the server); a dropped `tbody` leaves a descendant step.


### 12.2 Reading values: patterns, counts, every attribute, numbers (2026-09-25)

- `attr(name, pattern)` reads through a regex (its first group); a plan line offers `+pattern`
  and the pattern is editable in place (Python-style inline flags such as `(?i)` work in the
  preview too).
- A focused `.attr()` lists EVERY attribute across all the selected elements -- text, own text,
  `count` (children), the element's label, href / src, `data-*`, `aria-*`, class -- plus the
  numeric reads worth having ("as a number").
- Value ops on a read (package: `Field.number()`, `Field.map()`; the executor applies them to a
  plain read or a list of them): `number()` is the first number in the text (`£51.77` → 51.77,
  `In stock (22 available)` → 22) or a number word (`Three` → 3); `map({...})` looks a value up.
  A value node offers `.number()` and `.map({…})`.
- The star rating on books.toscrape.com (`<p class="star-rating Three">` with five icons -- the
  icon count is always 5): the records' shared fields offer `p.star-rating · class → number`,
  i.e. `select("p.star-rating").attr("class", "(?i)\b(zero|one|…|ten)\b").number()` → 3, and the
  icon count (`attr("count")`) beside it; prices come as numbers the same way.


## 13. The layout (2026-09-25): plan · action bar + page · output / picking

```
┌ toolbar: URL · tier · Start ··························· undo · Save · Export · Import · Run ▶ ┐
├ PLAN (280px) ─┬ ACTION BAR: .select_all("li.col-xs-6") ×20 · showing each li ×20 ── url ⟳ live ┬ OUTPUT / PICKING (340px) ┐
│ Reference(…)  │  + .select_all  + .select  + .attr  + .limit  + output ▾   [x] output  named ▾ │ idle: Rows · Run · Plan ·  │
│  .resolve()   ├────────────────────────────────────────────────────────────────────────────────┤ Page · Skeleton · MD · Code│
│   .select_all │                                                                                │                            │
│    .select    │                     THE PAGE  (the window's height; real render)                │ picking: suggestions, or   │
│     .attr → x │                                                                                │ the selector editor with   │
│               │                                                                                │ ONE button: Add .op("…")   │
└───────────────┴────────────────────────────────────────────────────────────────────────────────┴────────────────────────────┘
```

- **The plan** is the literal chain only: no type tags (the op implies the object); an output shows
  as a small `→ name` chip. Nothing is edited there but the arguments.
- **The action bar** (above the page) is where the focused line is acted on: its ops (`+ .select_all`,
  `+ .select`, `+ .attr`, `+ .number()`, `.resolve(…)`, `.paginate`, the page's ops), the `+ output ▾`
  menu (the fields the records share, every attribute, numbers), the pager for a page, and OUTPUT:
  a checkbox, and only when it is an output, how it is named -- a name, a column beside it, or a
  selector read off the page.
- **Picking**: adding an op that needs a selector turns the bar amber ("choose the selector…",
  cancel); the right column shows suggestions; a click on the page opens the selector editor there
  (parents with class toggles, candidates, the selector and its count, optional outputs read off
  it) with ONE button -- `Add .select_all("…")` -- the op is already chosen. Esc cancels (an op left
  without a selector is removed).
- **The page** gets the window's height and ~60% of its width; what it shows is the records of the
  nearest collection at or above the focus (a value or an element is seen in its record, outlined).
- Denser throughout: 10.5–12px type, one-line bars.


### 13.1 Denser, rows under the page, dates (2026-09-25)

- Layout now: the PLAN on the left (collapsible to a rail); in the middle the ACTION BAR, the PAGE,
  and the ROWS under it (collapsible) -- `Rows` is the local preview, `Run ▶` the server's rows
  (a JSON toggle); a right panel appears only while an op waits for its selector (suggestions, or
  the selector editor with its one Add button). The Plan / Page / Skeleton / MD / Code tabs are gone.
- Outputs are not tagged in the plan: an output line is framed in its colour, and the same colour
  heads its column in the rows (and outlines it on the page). Counts show only on collections and
  pages. Smaller type (10.5–11px) and tighter spacing throughout.
- The page shows the nearest `select_all` (or the page) around a focused select / value, its own
  matches outlined inside.
- `Field.date()` / `.datetime()` (package; `python-dateutil` is now a dependency, the fallback
  after the deterministic readings): ISO out; the suggestions offer "→ date" for values that are
  mostly a date, and "→ number" only for values that are mostly a number (a title with "40" in it
  is not offered as a number).


### 13.2 Collapsing, re-editing, counts, flatten (2026-09-25)

- Plan branches collapse (▾/▸, "+N" hidden lines); 10px type, tighter rows (and a denser rows table).
- Arguments in the plan are TEXT (the whole value on hover): clicking one reopens the selector
  panel on the element its selector matches, starting from the current selector (the button reads
  **Update**); a manual field (selector, or attribute + pattern) applies a typed value.
- Counts: `select_all` shows its matches (`×20`); a `select` under a collection shows how many
  records have it (`4/5`) -- red when not all do and it is not optional (the run would fail on the
  others); clicking it toggles `optional=True` (`4/5 opt`).
- Flatten (package: `project(flatten=[…] | True, sep=".")`, eager and streamed): a nested output (a
  followed page, a collection's rows / merged dict) has a **flatten** checkbox; its keys join the
  parent row as `detail.outcome`, `detail.info.UPC`…
- **Saving a run as a trace**: `POST /execute` takes `trace: "<name>"` and records the run as one
  trace file under `traces_dir` (every event, the plan in its footer; `trace` comes back in the
  response). The rows header has **⏺ trace** beside Run ▶ (it asks for a name) and links to the
  saved trace, which opens in Traces with its replay, events and plan.


## 14. The Run workspace (2026-09-25)

Run ▶ in Author opens **Run** with the plan. The server executes it in the background
(`POST /runs`: rows stream, every bus event is kept, a trace is written; `GET /runs/{id}?rows=&events=`
returns what arrived since the counts the client holds; each row carries the event index it
arrived at). The workspace shows:

- **the plan as stages** -- an EXPLAIN tree (FETCH, PAGES, EACH, FIND, READ, CAST, COLUMNS, EMIT…;
  a record's columns and a followed page branch under COLUMNS) that realises live: each stage's
  count so far, lit while active, dim until reached, ✕ where it failed (attributed from the
  executor's `plan.step` events and the error ledger; identical stages share their events,
  untraced stages count as their parent);
- **the output** as rows stream in; **errors** (click to jump there) and the **event log**
  (click a stage to see only its events);
- **a timeline**: ⏮ ◀ slider ▶ ⏭ -- the stages, the rows and the log as they were at any event;
  "live" follows the run;
- the run's **trace** (replay, events, plan in Traces), **run again**, and the runs held.

Also: the static render strips widgets that cannot work in a sandboxed copy (reCAPTCHA, hCaptcha,
Turnstile, analytics) -- the "could not connect to the reCAPTCHA service" notice came from one; a
pick inside a **shadow DOM** says so and offers the browser tier (its capture folds shadow DOM and
same-origin frames into the page); a picked **iframe** offers its page as a Document
(`select(iframe).attr("src").resolve()`).

### 14.1 Run as a pipeline graph (2026-09-25)

User decisions: Run shows the plan as a graph in the manner of Prefect / Dagster; it **loads a plan
without running it** (Run ▶ starts it; Author's button is "Open in Run", and a plan or blob can be
pasted); fan-out must stay readable at 50–5,000 items; resources, concurrency and what runs in
parallel are shown; actions are coloured by what they do. The onboarding-pipeline tab is deferred.

- **Cards.** A card is a run of steps that happen together (select · attr · number). A fetch or a
  fan-out starts its own card. Whole-collection ops (extract, merge, project) are small join pills.
  An alias's name read rides on the card it names. Each card shows its output name, its ops, and
  its count against the expected count.
- **Colour.** The left band and badge give the action: fetch, fan-out, find, read, interact, shape.
  The border gives the state: waiting, running, done, failed.
- **Fan-out at scale.** The cards that run once per item sit in a dashed **lane** labelled
  "per item ×N over R runs · K at once (http slots | browser pages)". One lane stands for every
  item. The fan-out card bins its items into at most 36 cells: done, in flight, waiting. Fan-out
  edges draw as a bundle with a ×N badge.
- **Resources.** The executor publishes `plan.parallel` (`n`, `limit`, `bound`) when a fan-out
  starts, and `plan.fanout` (`n`) when select_all / links / paginate return. A run samples the
  pool while live as `resources` events. The graph shows http slots and browser pages in use,
  the queue, and a sparkline.
- **Navigation.** Wheel or pinch zooms about the pointer, drag pans, arrows / + / − / 0 work from
  the keyboard, "fit" re-fits, and ⤢ maximises the graph over the rows.
- **Author.** The rows bar has a **Graph** popup with the same view and nothing run.
- **Rows.** Long text is clamped to two lines (click to expand). Nested objects and lists fold to
  a one-line preview (click to open). This applies in Author and Run.
- **Traces** list each trace's size on disk (`GET /traces` returns `bytes`).
