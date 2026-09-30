---
name: news
title: Latest news articles
search: latest news
max_pages: 15
prefer_api: true
schema:
  - headline: {type: string, description: the article's headline / title}
  - url: {type: url, description: the link to the full article page}
  - published: {type: datetime, description: the publish date and time (ISO 8601 if available)}
  - author: {type: string, description: the byline / author name}
  - section: {type: string, description: the news section or category (e.g. Business, World)}
  - body: {type: string, description: the full article body text (follow the article link to get it)}
optional: [author, section]
---
The latest news articles on the site, in DETAIL: each story's headline, the link to the full
article, the publish date/time, the author, the section, and the full article body text (follow
each article's link to read and extract its body -- not just the listing summary).
