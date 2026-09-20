"""Case study: discover a site's real sitemap.xml and map a slice of it.

Site: https://webscraper.io/ -- a scraping-tools company's own site, which
publishes a real ``sitemap.xml`` and is a fair target for a mapping demo.

What it does: (1) discover the site's declared sitemap URLs (reads robots.txt for
``Sitemap:`` directives, falls back to /sitemap.xml, expands a <sitemapindex> one
level); (2) map a bounded slice -- a single-domain crawl that honours the real
sitemap by seeding the frontier from it -- and show a lean summary per page.

Features: sitemap() discovery, crawl() seeded from the sitemap.
Run:  env/bin/python examples/sitemap_mapper.py
"""

from __future__ import annotations

from webclient import WebClient, WebException

SITE = "https://webscraper.io/"
UA = "webclient-examples/0.1"


def discover(wc: WebClient) -> None:
    print("== discovery: real sitemap.xml URLs ==")
    refs = list(wc.sitemap(SITE))  # reads robots Sitemap: dirs / sitemap.xml, expands the index
    print(f"discovered {len(refs)} URLs from the sitemap")
    for r in refs[:8]:
        print(f"  {r.url}")
    if len(refs) > 8:
        print(f"  … and {len(refs) - 8} more")
    print()


def map_slice(wc: WebClient) -> None:
    print("== map a bounded slice (seeded from the sitemap) ==")
    seeds = wc.sitemap(SITE)  # feed the real sitemap URLs straight into a bounded crawl
    with wc.crawl(seeds, max_pages=8, depth=1, obey_robots=True, browser=False) as crawl:
        crawl.run()
        print(f"mapped {len(crawl.pages)} pages, done={crawl.done}")
        for page in crawl.pages:  # PageCards (the default crawl projection)
            print(f"  {(page.title or '?')[:44]:<44}  {page.final_url or page.url}")


def main() -> None:
    with WebClient(default_headers={"User-Agent": UA}, min_interval=0.4, timeout=25) as wc:
        discover(wc)
        map_slice(wc)


if __name__ == "__main__":
    try:
        main()
    except WebException as exc:
        print(f"run failed: {exc.error.type} -- {exc} (retriable={exc.error.retriable})")
