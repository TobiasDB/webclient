---
name: news
title: News articles
args: [site]
search:
  term: "{site} news"
  k: 10
  domain: ["{site}"]
  path: ["news", "latest", "world", "stories"]
  max_pages: 8
look:
  - "the news site's own index / section page listing its latest articles, newest first"
ignore:
  - a single article page, an aggregator or a search engine's copy, a topic tag page with a handful of items
expect_rows: "10-100"
schema:
  - headline: {type: string, description: the article's headline}
  - published: {type: datetime, description: when the article was published (a time element or a label)}
  - url: {type: url, description: the link to the article}
  - section: {type: string, description: the section / topic label shown in the listing}
optional: [section, published]
---
The latest news articles listed on {site}: the headline, when each was published, its section and the link to the article.
