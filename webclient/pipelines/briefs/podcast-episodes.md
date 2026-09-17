---
name: podcast-episodes
title: Podcast episodes
search: podcast episodes
schema:
  - title: the episode title
  - date?: the publish date, when shown (e.g. 2026-09-14, Sep 14 2026)
  - url?: a link to the episode
  - number?: the episode number, if shown (e.g. 142, S3E5, #88)
look:
  - the podcast episodes / archive page
  - the podcast RSS feed if one is exposed
ignore:
  - marketing, about, sponsor and blog pages
crawl:
  max_pages: 12
  depth: 2
  browser: auto
---
Every episode the podcast lists, each with its title, the publish date and episode number when
shown, and a link to the episode. Prefer the episodes listing or the podcast RSS feed.
