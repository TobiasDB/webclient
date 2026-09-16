---
name: countries
title: Countries
schema:
  - name: the country name
  - capital?: the capital city, when shown
  - population?: the population, when shown
  - area?: the land area, when shown
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
