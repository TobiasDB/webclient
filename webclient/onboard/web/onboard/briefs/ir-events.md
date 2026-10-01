---
name: ir-events
title: Investor-relations events (upcoming + archived)
args: [company]
search:
  term: "{company} investor relations events calendar"
  terms: ["{company} investor relations", "{company} investors"]
  k: 10
  domain: ["{company}", "investors.{company}", "ir.{company}", "investor.{company}", "q4cdn", "gcs-web"]
  path: ["event", "calendar", "webcast", "presentation", "investor", "ir"]
  max_pages: 12
look:
  - "{company}'s OWN investor-relations events / calendar page (investors.<company>.com, ir.<company>.com) listing upcoming and past events"
ignore:
  - third-party finance aggregators and data vendors (Benzinga, MarketScreener, Yahoo Finance, Quartr, Seeking Alpha, TipRanks), stock exchanges, brokerages, a single event or news page
expect_rows: "3-200"
schema:
  - title: {type: string, description: the event name (Q3 2026 Earnings Call, Annual Meeting)}
  - datetime: {type: datetime, description: the event date and time, with timezone if shown}
  - kind: {type: string, description: the event type — earnings call, conference, annual meeting, investor day}
  - event_url: {type: url, description: the link to the event's own page}
  - webcast_url: {type: url, description: the live / replay webcast or registration link}
optional: [kind, event_url, webcast_url]
queries:
  - name: upcoming
    hint: "ONLY the UPCOMING / future events (the section or list headed Upcoming, or records whose date is in the future). A webcast / registration link is a separate <a> in the record."
  - name: past
    hint: "ONLY the PAST / ARCHIVED events (the section or list headed Past / Archive, or records whose date has passed). A replay / webcast link is a separate <a> in the record."
---
Every investor-relations event {company} lists — upcoming and archived: the event name, its date and time, its type, the link to its own page and the webcast link.
