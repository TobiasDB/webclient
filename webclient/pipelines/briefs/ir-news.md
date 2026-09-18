---
name: ir-news
title: Investor-Relations News
search: investor relations news
schema:
  - title: the headline of the news item / press release
  - date: the publication date (as shown on the page)
  - url: a link to the full item
  - category?: the item's tag or category, if shown (e.g. Earnings, Product)
  - summary?: a one-line teaser or summary, when present
  - links?: every URL attached to the item, as a LIST (the item plus any PDF/webcast/related links) — collect all of them with .select_all(...)
look:
  - the investor relations (IR) section, press releases and news / newsroom pages
  - a press-release or news data API / RSS feed if one is exposed
ignore:
  - marketing, product, careers, blog and support pages
  - SEC filings and financial statements (that is a different dataset)
crawl:
  max_pages: 25
  depth: 3
  browser: auto
---
The company's investor-relations news feed: every press release / news item it has
published to investors, each with a headline, publication date, a link to the full
item, its category, and a short summary when one is shown. Prefer a news/press-release
API, RSS feed, or a single paginated listing over scattered individual pages.
