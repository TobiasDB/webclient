---
name: blog
title: Blog posts
schema:
  - title: the post title
  - date: the publication date
  - url: a link to the full post
  - author?: the post author, when shown
  - summary?: a one-line teaser / excerpt, when present
look:
  - the blog / news / articles listing page
  - a blog RSS/Atom feed if one is exposed
ignore:
  - marketing, pricing, docs and careers pages
crawl:
  max_pages: 15
  depth: 2
  browser: auto
---
Every blog / news post listed, each with its title, publication date, a link to the full
post, and the author and a short summary when shown. Prefer a single listing or the blog's
RSS feed.
