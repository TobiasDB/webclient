---
name: ir-events
title: Investor-relations events (the latest: upcoming and the current period)
args: [company]
related: [ir-news]
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
optional: [datetime, kind, event_url, webcast_url]
hints:
  author_extract: "The LATEST events only: the UPCOMING section and the current year's / current period's list -- not the archive, not older years' tabs (a separate brief captures those). The date is often a SIBLING paragraph after the title (`+ p`); the latest entries may show no date yet. A webcast / registration link is a separate <a> in the record."
---
The LATEST investor-relations events {company} lists — the upcoming ones and the current period's — the event name, its date and time, its type, the link to its own page and the webcast link. Archived / older years are a separate dataset.
