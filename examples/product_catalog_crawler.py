"""Case study: a product-catalogue crawler with typed extraction.

Site: https://books.toscrape.com/ -- a sandbox built explicitly for scraping
practice, so it is the polite place to demonstrate a real crawl.

What it does: (1) bounded, same-origin crawl of the catalogue -- each page comes
back as a lean summary (transport + metadata by default, cheap at crawl scale);
(2) structured extraction on a listing page: every product -> a typed `Book`
model via `select_all().extract().project(Book)`.

Features: crawl (auto, robots-obeying, bounded), select_all/extract/project(Model).
Run:  env/bin/python examples/product_catalog_crawler.py
"""

from __future__ import annotations

from pydantic import BaseModel

from webclient import WebClient, WebException, wq

CATALOG = "https://books.toscrape.com/"
UA = "webclient-examples/0.1 (+https://github.com/TobiasDB/webclient)"


class Book(BaseModel):
    title: str = ""
    price: str = ""
    availability: str = ""


def crawl_catalog(wc: WebClient) -> None:
    print("== bounded crawl of the catalogue ==")
    with wc.crawl(CATALOG, auto=True, max_pages=6, depth=2, obey_robots=True) as crawl:
        crawl.run()
    print(f"fetched {len(crawl.pages)} pages, {len(crawl.frontier)} still queued, "
          f"done={crawl.done}")
    for page in crawl.pages:
        title = page.metadata.title if page.metadata else "?"
        status = page.transport.status_code if page.transport else "?"
        print(f"  [{status}] {title}")
    print()


def extract_products(wc: WebClient) -> None:
    print("== typed extraction on the listing page ==")
    listing = wc.fetch(CATALOG)
    books = (
        listing.select_all(".product_pod")
        .extract(
            title=wq.doc.select("h3 a").attr("title"),
            price=wq.doc.select(".price_color").text_content,
            availability=wq.doc.select(".availability").text_content,
        )
        .project(Book)  # each row validated into a Book
    )
    print(f"extracted {len(books)} books; first 5:")
    for b in books[:5]:
        print(f"  {b.price:>8}  {b.availability.strip():<12}  {b.title}")


def main() -> None:
    with WebClient(default_headers={"User-Agent": UA}, min_interval=0.3, timeout=25) as wc:
        crawl_catalog(wc)
        extract_products(wc)


if __name__ == "__main__":
    try:
        main()
    except WebException as exc:
        print(f"run failed: {exc.error.type} -- {exc} (retriable={exc.error.retriable})")
