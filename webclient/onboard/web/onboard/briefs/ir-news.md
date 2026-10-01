---
name: ir-news
title: Investor-relations news / press releases
args: [company]
related: [ir-events]
search:
  term: "{company} investor relations press releases"
  terms: ["{company} investor relations", "{company} investors"]
  k: 10
  domain: ["{company}", "investors.{company}", "ir.{company}", "investor.{company}", "q4cdn", "gcs-web"]
  path: ["news", "press", "release", "investor", "ir"]
  max_pages: 12
look:
  - "{company}'s OWN investor-relations news / press-release listing (investors.<company>.com, ir.<company>.com, a Q4 / GlobeNewswire-hosted IR site): a paginated list of releases, newest first"
ignore:
  - third-party wire / aggregator copies (PR Newswire, Business Wire, GlobeNewswire's own site, Yahoo Finance, Benzinga, MarketScreener, Seeking Alpha), stock exchanges, brokerages, a single release page
expect_rows: "5-200"
schema:
  - headline: {type: string, description: the release's headline / title}
  - published: {type: datetime, description: the release date (and time if shown)}
  - category: {type: string, description: the release category / tag if shown}
  - url: {type: url, description: the link to the release's own page}
optional: [category]
hints:
  author_extract: "Releases are newest first; a row carries the headline (usually the link text), the date (often a <time> element), sometimes a category tag."
---
Every press / news release {company} published through its investor-relations site, newest first: the headline, the date, the category and the link to the release's own page.
