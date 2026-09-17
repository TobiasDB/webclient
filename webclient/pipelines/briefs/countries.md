---
name: countries
title: Countries
search: list of countries
schema:
  - name: the country name
  - capital?: the capital city, when shown (e.g. Paris, Tokyo)
  - population?: the population, when shown (e.g. 67,390,000)
  - area?: the land area, when shown (e.g. 551,695 km², 244820)
look:
  - a page or table listing countries and their facts
  - a countries data API / JSON endpoint if one is exposed
ignore:
  - marketing, about and navigation pages
crawl:
  max_pages: 10
  depth: 2
  browser: auto
---
Every country listed, each with its name and, when shown, its capital, population and area.
Prefer a single listing/table or a countries data API.
