---
name: github-releases
title: Software releases
search: github releases
schema:
  - version: the release version / tag name (e.g. v1.4.0)
  - date: the release / published date (e.g. Sep 9 2026, 2026-09-09)
  - url?: a link to the release
  - summary?: the release title or a one-line summary, when shown
look:
  - a GitHub releases page or its .atom feed, a downloads/releases page, or a changelog
  - a releases RSS/Atom feed or JSON API if exposed
ignore:
  - source code, issues, pull requests and documentation pages
crawl:
  max_pages: 12
  depth: 2
  browser: auto
---
Every published release of the software, each with its version/tag, the release date, a link
to it, and the release title/summary when shown. An RSS/Atom releases feed or a releases API
is preferred over the rendered listing.
