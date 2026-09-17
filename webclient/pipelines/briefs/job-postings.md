---
name: job-postings
title: Job postings
search: careers job openings
schema:
  - title: the job / role title (e.g. Senior Backend Engineer, Product Designer)
  - location: where the role is based (e.g. London, Remote — US, Berlin/Hybrid)
  - department?: the team or department, if shown (e.g. Engineering, Sales, Design)
  - url?: a link to the full job posting
look:
  - the careers / jobs / open-roles page, or a jobs board (Greenhouse, Lever, Ashby)
  - a jobs data API / JSON endpoint if one is exposed
ignore:
  - marketing, product, blog and investor pages
crawl:
  max_pages: 15
  depth: 2
  browser: auto
---
Every open role the company lists, each with its title, location, the department/team when
shown, and a link to the full posting. Prefer a single jobs listing or a jobs API/board over
scattered individual role pages.
