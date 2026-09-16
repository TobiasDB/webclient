---
name: changelog
title: Product changelog
schema:
  - date: the date of the changelog entry
  - title: the entry's headline / what changed
  - version?: a version number, if the entry has one
  - summary?: a one-line description of the change, when present
look:
  - a changelog / what's new / release-notes / updates page
  - a changelog RSS feed or JSON API if exposed
ignore:
  - marketing, pricing, blog and docs pages
crawl:
  max_pages: 12
  depth: 2
  browser: auto
---
Every changelog / release-notes entry the product publishes, each with its date, a headline of
what changed, a version when one is shown, and a short summary. Prefer a single changelog
listing or a changelog feed.
