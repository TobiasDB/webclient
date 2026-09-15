# examples — live case studies

Runnable case studies that use the `webclient` library against **real websites**,
each showcasing a feature (or two) end to end. They are documentation you can run,
not tests — they hit the live internet, so output changes over time and a site may
be down.

Every example targets a **scraper-friendly** site on purpose:

| example | site | why it's a good citizen | features |
|---|---|---|---|
| `news_article_scraper.py` | text.npr.org | NPR's text-only edition: tiny pages, stable markup, meant for low-bandwidth/accessibility | fetch, `render("text"/"markdown")`, `summary()`, select/attr, Reference resolve |
| `product_catalog_crawler.py` | books.toscrape.com | a sandbox built explicitly for scraping practice | `crawl`, `select_all`/`extract`/`project(Model)` |
| `lazy_expression_extract.py` | quotes.toscrape.com | scraping sandbox | lazy `Expr`, `to_blob`/`from_blob`, `project(Model)`, pagination |
| `sitemap_mapper.py` | webscraper.io | a scraping-tools company's own site (real `sitemap.xml`) | `discover_sitemaps()` + `sitemap()` map |
| `live_browser_events.py` | quotes.toscrape.com/js | JS-rendered sandbox page | live browser render, `events`, `summary().runtime` |
| `summary_llm_view.py` | mixed | — | `summary()` as an LLM's token-lean view of a page |
| `error_handling.py` | httpbin.org (+ local guards) | httpbin exists to return chosen statuses/delays | structured errors, `optional=`, select-miss, SSRF/robots |

## Run

```bash
env/bin/python examples/news_article_scraper.py
```

Each script is standalone (copy-paste friendly), sets a descriptive `User-Agent`,
spaces its requests (`min_interval`), obeys `robots.txt` where it crawls, and wraps
its network work so a hiccup prints a clean message instead of a traceback.

## Be a good citizen

Keep `max_pages` small, leave `min_interval` in place, and don't point the crawler
at sites that haven't invited it. `error_handling.py` deliberately triggers failures
(404/500/timeout/blocked host) to show how they surface — that is the point of it.
