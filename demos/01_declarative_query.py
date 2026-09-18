"""01 · A query is DATA, not a script.

THE POINT (vs Playwright): a webclient extraction is a declarative, typed, serialisable
PLAN -- three lines that describe WHAT you want. The equivalent Playwright code is an
imperative script: launch a browser, wait, loop, query, read text, handle misses by hand.
The plan can be stored, versioned, shipped over the wire, and re-run without the code that
built it. A Playwright script is code you must keep, run and babysit.
"""

from webclient import WebClient, wq
from _site import serve, h1, kv, table

# The equivalent Playwright, for contrast (≈20 lines of imperative browser driving):
PLAYWRIGHT = """\
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    b = p.chromium.launch(); page = b.new_page()   # always a full browser
    page.goto(url); rows = []
    for card in page.query_selector_all(".card"):
        title = card.query_selector(".title").inner_text()
        price_el = card.query_selector(".price")
        price = price_el.inner_text() if price_el else None
        href = card.query_selector("a").get_attribute("href")
        rows.append({"title": title, "price": price, "link": href})
    b.close()"""


def main() -> None:
    base = serve()

    h1("The whole extraction, declaratively")
    query = (
        wq.reference(base)
        .resolve()                                   # fetch (cheapest tier; no browser here)
        .select_all(".card")                         # one row per card
        .extract(
            title=wq.doc.select(".title").attr("text"),
            price=wq.doc.select(".price").attr("data-price"),  # the value lives in the attr
            link=wq.doc.select("a").attr("href"),    # a Reference you could follow
        )
        .project()
    )
    print("  " + query.describe())

    with WebClient() as wc:
        rows = query.collect(wc.ref(base))

    h1("Result")
    table(rows)

    h1("The same thing in Playwright (imperative, always a browser)")
    for line in PLAYWRIGHT.splitlines():
        print("  " + line)

    h1("Why it matters")
    kv("declarative", "you describe the data; the library plans the fetch + extraction")
    kv("serialisable", "query.to_blob() is a portable artifact -- store it, ship it, re-run it")
    kv("no browser", "this ran over plain HTTP; Playwright always launches chromium")
    kv("loud misses", "a missing required field raises at authoring time, not silently None")


if __name__ == "__main__":
    main()
