---
name: ir-sec-filings
title: SEC filings (from the investor-relations site)
search: investor relations SEC filings
max_pages: 10
prefer_api: true
look:
  - the company's OWN investor-relations SEC-filings page (e.g. investors.<entity>.com,
    ir.<entity>.com, <entity>.gcs-web.com, <entity>.q4cdn.com — "SEC Filings", "Financials &
    Filings", "Filings & Reports"), a table of filings newest-first with per-filing document links
ignore:
  - third-party filing aggregators and data vendors (Yahoo Finance, MarketScreener, Seeking Alpha,
    TipRanks, WhaleWisdom, Last10K), brokerages, and a single filing's own document; EDGAR itself is
    acceptable only when the company's IR site has no filings page
author_hint: >
  Filings are a TABLE (or a list of rows) newest-first: one row per filing with the form type
  (10-K, 10-Q, 8-K, DEF 14A, S-8, 4, ...), the filing date, a description/title, and one or more
  document links (HTML viewer, PDF, XBRL/XLS). The table is usually filtered by year and/or form
  type via controls — capture the CURRENT/default set (all forms, latest year), not one form type's
  subset. Read the form type and date from their own cells; the document links are the row's
  anchors. Many IR sites load the table from a JSON filings API — prefer it when the page has one.
review_hint: >
  Be strict on form coverage and recency: the rows must span the filing TYPES (periodic 10-K/10-Q,
  current 8-K, proxy, ownership forms) not one type's filter view, and the newest row must be a
  recent filing (weeks to a few months old for a listed company) — reject a single-form or an
  archived-year sample. Every row must carry its form type and filing date.
key: [filed, form, description]
schema:
  - form: {type: string, description: the SEC form type (10-K, 10-Q, 8-K, DEF 14A, S-8, 4, 13F-HR, ...)}
  - filed: {type: datetime, description: the filing date (ISO 8601 if possible)}
  - description: {type: string, description: the filing's title / description as listed}
  - period: {type: string, description: the reporting period or period end date, if shown}
  - filing_url: {type: url, description: the link to the filing's page or HTML viewer}
  - document: {type: document, description: the primary filing document (PDF or HTML) as a downloadable file}
  - xbrl: {type: document, description: the XBRL / XLS financial data attachment, if offered}
optional: [description, period, xbrl]
---
The company's SEC filings as listed on its investor-relations site: every filing, newest first,
across ALL form types (10-K, 10-Q, 8-K, proxy statements, ownership forms, registration statements)
— for each the form type, the filing date, the description, the reporting period, the link to the
filing, and the filing's primary document (PDF/HTML) as a downloadable `document`, plus the XBRL /
financial-data attachment when offered. Capture the current/default listing (all forms, the latest
year), not a single form type's filter view or an archived year.
