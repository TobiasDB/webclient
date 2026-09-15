"""Case study: discover a site's real sitemap.xml and map a slice of it.

Site: https://webscraper.io/ -- a scraping-tools company's own site, which
publishes a real ``sitemap.xml`` and is a fair target for a mapping demo.

What it does: (1) discover the site's declared sitemap URLs (reads robots.txt for
``Sitemap:`` directives, falls back to /sitemap.xml, expands a <sitemapindex> one
level); (2) map a bounded slice -- a single-domain crawl that honours the real
sitemap by seeding the frontier from it -- and show a lean summary per page.

Features: sitemaps() discovery, sitemap() map.
Run:  env/bin/python examples/sitemap_mapper.py
"""

from __future__ import annotations

from webclient import WebClient, WebException

SITE = "https://webscraper.io/"
UA = "webclient-examples/0.1 (+https://github.com/TobiasDB/webclient)"


def discover(wc: WebClient) -> None:
    print("== discovery: real sitemap.xml URLs ==")
    refs = list(wc.sitemaps(SITE))
    print(f"discovered {len(refs)} URLs from the sitemap")
    for r in refs[:8]:
        print(f"  {r.url}")
    if len(refs) > 8:
        print(f"  … and {len(refs) - 8} more")
    print()


def map_slice(wc: WebClient) -> None:
    print("== map a bounded slice (seeded from the sitemap) ==")
    site = wc.sitemap(SITE, max_pages=8, depth=1)
    print(f"mapped {len(site.pages)} pages, done={site.done}")
    for page in site.pages:
        title = page.metadata.title if page.metadata else "?"
        url = page.transport.final_url if page.transport else "?"
        print(f"  {title[:44]:<44}  {url}")


def main() -> None:
    with WebClient(default_headers={"User-Agent": UA}, min_interval=0.4, timeout=25) as wc:
        discover(wc)
        map_slice(wc)


if __name__ == "__main__":
    try:
        main()
    except WebException as exc:
        print(f"run failed: {exc.error.type} -- {exc} (retriable={exc.error.retriable})")
