"""Case study: an LLM-authored lazy expression, shipped as a compact blob.

Site: https://quotes.toscrape.com/ -- a scraping sandbox.

The scenario: an agent writes a *lazy expression* describing the extraction it
wants -- "resolve the page, take each .quote, pull its text + author" -- without
running anything. That plan serialises to a short, url-safe blob it can hand off
(store, log, send over the wire). Anyone can rebuild it from the blob, see it
pretty-printed, validate it (private names are refused), and only then run it.

Features: lazy Expr recording, to_blob()/from_blob(), explain(), project(Model),
pagination follow. Run:  env/bin/python examples/lazy_expression_extract.py
"""

from __future__ import annotations

from pydantic import BaseModel

from webclient import WebClient, WebException, from_blob, wq

START = "https://quotes.toscrape.com/"
UA = "webclient-examples/0.1"


class Quote(BaseModel):
    text: str = ""
    author: str = ""


def author_plan_blob() -> str:
    """The plan an LLM would emit -- pure recording, nothing runs here."""
    plan = (
        wq.ref.resolve()
        .select_all(".quote")
        .extract(
            text=wq.doc.select(".text").text_content,
            author=wq.doc.select(".author").text_content,
        )
        .project()
    )
    print("authored plan:")
    print("  explain:", plan.explain())
    blob = plan.to_blob()
    print(f"  blob ({len(blob)} chars): {blob}")
    return blob


def run_across_pages(wc: WebClient, blob: str, max_pages: int = 3) -> None:
    print("\nrebuild from blob + run, following pagination:")
    plan = from_blob(blob, wc)  # rebuilt + name-validated
    url, total = START, 0
    for page_no in range(1, max_pages + 1):
        rows = plan.collect(wc.ref(url))
        quotes = [Quote(**r) for r in rows]
        total += len(quotes)
        print(f"  page {page_no}: {len(quotes)} quotes  "
              f"e.g. {quotes[0].author!r}: {quotes[0].text[:48]!r}")
        nxt = wc.fetch(url).select(".next a", optional=True)
        if not nxt.ok:
            break
        url = nxt.attr("href").url
    print(f"  total: {total} quotes")


def main() -> None:
    blob = author_plan_blob()
    with WebClient(default_headers={"User-Agent": UA}, min_interval=0.3, timeout=25) as wc:
        run_across_pages(wc, blob)


if __name__ == "__main__":
    try:
        main()
    except WebException as exc:
        print(f"run failed: {exc.error.type} -- {exc} (retriable={exc.error.retriable})")
