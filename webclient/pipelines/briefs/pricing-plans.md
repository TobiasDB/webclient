---
name: pricing-plans
title: Pricing plans
schema:
  - plan: the plan / tier name (e.g. Free, Pro, Enterprise)
  - price: the plan's price as shown (e.g. $20, Custom)
  - period?: the billing period, if shown (e.g. per month, per year, per seat)
  - highlight?: the headline feature or tagline for the plan, when present (e.g. "Unlimited projects", "Best for teams")
look:
  - the pricing / plans page
ignore:
  - blog, docs, careers and investor pages
crawl:
  max_pages: 8
  depth: 2
  browser: auto
---
Every pricing plan / tier on the pricing page, each with its plan name, the price as shown, the
billing period when present, and the plan's headline feature/tagline. One page, several plans.
