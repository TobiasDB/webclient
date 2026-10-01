---
name: ir-news
title: Investor-relations news / press releases
search: investor relations press releases news
max_pages: 10
prefer_api: true
look:
  - the company's OWN investor-relations news / press-release listing (e.g. investors.<entity>.com,
    ir.<entity>.com, <entity>.gcs-web.com, <entity>.q4cdn.com — "News", "Press Releases",
    "News Releases", "Newsroom" under Investors), a paginated list of releases newest-first
ignore:
  - third-party wire/aggregator copies (PR Newswire, Business Wire, GlobeNewswire, Yahoo Finance,
    Benzinga, MarketScreener, Seeking Alpha), stock exchanges, brokerages, and a single release page
author_hint: >
  Releases are a repeating record region (a list or table of rows, newest first, usually paginated
  or behind a year filter — capture the CURRENT year's records, not an archived year's tab). Each
  row carries the headline, the date, often a category tag, and a link to the release's own page;
  the body text lives on that page — follow the link ONCE per record and read the body there. If
  the page has a JSON news API, prefer it. Many releases also attach a PDF of the release. The body
  is the FULL release text read from the release page's own content element (not a summary or a
  generic readable extraction).
identity_hint: >
  A release is identified by its text content: declare the release page's identity over the same
  content element the body is read from (e.g. detail_identity("article")).
review_hint: >
  Be strict on recency: the rows must be the LATEST releases (the newest row within the last few
  months for an active company) — reject a sample from an archived year or a category subset only.
  Each row must be a press/news release by the company itself, not a third-party article.
schema:
  - headline: {type: string, description: the release's headline / title}
  - published: {type: datetime, description: the release date (and time if shown; ISO 8601 if possible)}
  - category: {type: string, description: the release category / tag if shown (e.g. Financial, Corporate, Product)}
  - summary: {type: string, description: the teaser / first paragraph shown in the listing, if any}
  - url: {type: url, description: the link to the release's own page}
  - body: {type: string, description: the full release text, from the release's own page}
  - pdf: {type: document, description: the PDF version of the release when one is attached}
optional: [category, summary, pdf]
---
The company's investor-relations news: every press release / news release it published, newest
first, in DETAIL — the headline, the date, the category, the link to the release's own page, and
the full body text (follow each release's link ONCE to read the body; the listing shows only a
teaser). Also capture the attached PDF of the release as a downloadable `document` when one is
offered. The dataset must reflect the LATEST releases, not an archived year.
