---
name: news
title: Latest news articles
search: latest news
max_pages: 10
prefer_api: true
author_hint: >
  The body is the FULL article: on the article's own page, read the article's content element
  (the <article> / main article body container) as text -- the whole article, not a summary or a
  generic readable extraction. Captions, bylines and "share" chrome that sit outside that element
  stay out.
identity_hint: >
  An article is identified by its article text content: declare the detail page's identity over
  the same content element the body is read from (e.g. detail_identity("article")), so a changing
  clock, sidebar or related-links box does not make it a new document.
schema:
  - headline: {type: string, description: the article's headline / title}
  - url: {type: url, description: the link to the full article page}
  - published: {type: datetime, description: the publish date and time (ISO 8601 if available)}
  - author: {type: string, description: the byline / author name}
  - section: {type: string, description: the news section or category (e.g. Business, World)}
  - body: {type: string, description: the full article text from the article's own content element on its page (follow the article link; the whole article, not a summary)}
optional: [author, section]
---
The latest news articles on the site, in DETAIL: each story's headline, the link to the full
article, the publish date/time, the author, the section, and the full article body text (follow
each article's link to read and extract its body -- not just the listing summary).
