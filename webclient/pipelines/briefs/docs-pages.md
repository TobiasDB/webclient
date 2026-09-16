---
name: docs-pages
title: Documentation pages
schema:
  - title: the doc page / section title
  - url: a link to the page
  - section?: the parent section / category in the nav, when shown
look:
  - a documentation site's navigation / table of contents / API reference index
ignore:
  - marketing, pricing, blog and careers pages
crawl:
  max_pages: 10
  depth: 2
  browser: auto
---
Every documentation page listed in the docs navigation / table of contents, each with its
title, a link to it, and the parent section when shown.
