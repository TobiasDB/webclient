---
name: products
title: Product catalogue
search: products
prefer_api: true
schema:
  - name: the product name / title
  - price: the listed price (with currency if shown)
  - url: the product detail-page link
  - image: the product image URL
optional: [price, image]
---
The product catalogue: every product listed, with its name, price, detail-page link and
image. Prefer a JSON/data-API behind the listing when one backs the page.
