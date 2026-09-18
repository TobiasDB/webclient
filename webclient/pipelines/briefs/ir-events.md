---
name: ir-events
title: Investor-Relations Events
search: investor relations events presentations webcasts
exit_when: the UPCOMING events section is empty (no upcoming events are scheduled). We can't author a reliable query for a section with no example rows, so stop here rather than guess its structure.
hints: This is a SPLIT dataset with two sections that do NOT share one record format. ARCHIVED / PAST events are a repeating list, often TABBED BY YEAR (an older year may show by default). The UPCOMING section is separate and is frequently a SINGLE row (one scheduled event) laid out DIFFERENTLY from the archived rows — a callout/banner, not a table row — so a .select_all(...) tuned to the archived rows will silently MISS it. Write a split query that captures the upcoming section separately from the archived list (a sub-list per section, or a .step across the year tabs), and treat the single upcoming row as its own selector. Mark upcoming-only fields optional; if upcoming is truly empty the exit condition applies.
schema:
  - title: the event name (e.g. "Q3 FY2026 Earnings Call", "Adobe Investor Meeting 2026", "Morgan Stanley Technology Conference")
  - date: the event date as shown (e.g. "September 11, 2026", "Dec 12, 2026", "2026-09-11")
  - url?: a link to the event — its webcast / registration / replay / detail page, when present
  - type?: the kind of event, if shown (e.g. Earnings Call, Investor Day, Conference, Webcast)
  - time?: the event time, when shown, with timezone (e.g. "2:00 PM PT", "5:00 p.m. ET")
  - status?: whether the event is upcoming or past, if the section/page makes it clear (e.g. "upcoming", "past")
  - materials?: every URL attached to the event, as a LIST — webcast, replay, slides/presentation, transcript — collect them all with .select_all(...)
look:
  - the investor relations (IR) "Events", "Events & Presentations", or "Events Calendar" page
  - BOTH the UPCOMING and the PAST / ARCHIVED events sections — capture both (they are usually two separate lists or tabs on the same page)
  - an events / webcast data API or feed if one is exposed
ignore:
  - press releases and news items (that is the separate ir-news dataset)
  - SEC filings and financial statements (a different dataset)
  - marketing, product, careers, blog and support pages
crawl:
  max_pages: 25
  depth: 3
  browser: auto
---
The company's investor-relations EVENTS: every investor event it lists — earnings calls,
investor days/meetings, conference appearances, webcasts and presentations — each with its
title, date and a link, plus (when shown) its type, time and links to materials
(webcast / replay / slides / transcript).

Capture BOTH sections — this is a SPLIT dataset. The page almost always has two separate
lists: "Upcoming events" and "Past events" (often on tabs, or the past ones behind a year
filter). Select records across BOTH so the dataset is complete; do not scope the query to
only one of them.

The UPCOMING section is frequently EMPTY (no events are currently scheduled) — that is
NORMAL, not a failure. A complete, valid result can be an empty upcoming list plus a
populated past/archived list. Mark upcoming-only fields optional and never drop the past
events just because upcoming has none. Prefer an "Events & Presentations" listing (or an
events/webcast API) over scattered per-event pages.
