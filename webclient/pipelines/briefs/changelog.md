---
name: changelog
title: Product changelog
search: changelog release notes
schema:
  - date: the date of the changelog entry (e.g. 2026-09-14, Sep 14 2026)
  - title: the entry's headline / what changed (e.g. "Dark mode", "Fixed CSV export")
  - version?: a version number, if the entry has one (e.g. v2.3.0, 2026.9.1)
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
