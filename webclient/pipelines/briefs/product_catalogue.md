---
name: product-catalogue
title: Product Catalogue
schema:
  - name: the product's display name
  - sku: the stock-keeping unit / product code, if shown
  - url: a link to the product's own detail page
  - price: the price, as a structured object
  - price.value: the numeric amount only (e.g. 30)
  - price.unit: the currency or unit (e.g. $, USD, EUR)
  - price.modifiers: any qualifiers on the price (e.g. "per 1TB", "per month")
look:
  - product listing, catalogue and pricing pages
  - a data API returning the whole catalogue (prefer this over a rendered page)
ignore:
  - blog, news, careers, legal and support pages
crawl:
  - max_pages: 30
  - depth: 2
  - browser: false
---
The company's full product catalogue: every product it sells, each with a name,
SKU, a link to its detail page, and a structured price (value, unit and any
modifiers). Prefer a queryable data API returning the whole catalogue over a
paginated listing page or a search box.
