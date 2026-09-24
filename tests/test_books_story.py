"""The books story end to end, offline: a catalogue of records, each linking to a detail page
with a description and a key/value table; the plan the Author workspace builds -- pages,
records, fields, a per-record drill-down whose table becomes a dict -- yields every book
with its details. (The public books.toscrape.com has the same shape.)"""

from webclient import WebClient
from webclient.interface import wq

BOOK = """<html><body><article class="product_page"><h1>{title}</h1>
<div id="product_description"></div><p>{desc}</p>
<table class="table"><tr><th>UPC</th><td>{upc}</td></tr><tr><th>Price (incl. tax)</th><td>£{price}</td></tr><tr><th>Availability</th><td>In stock ({n} available)</td></tr></table>
</article></body></html>"""


def _catalogue(page: int, per: int = 3, pages: int = 2) -> str:
    items = "".join(
        f'<li class="col-xs-6"><article class="product_pod"><h3><a href="/book/{i}" title="Book {i}">Book {i}…</a></h3>'
        f'<p class="price_color">£{i}.00</p><p class="star-rating Three"><i></i></p></article></li>'
        for i in range((page - 1) * per + 1, page * per + 1)
    )
    nxt = f'<li class="next"><a href="/catalogue/page-{page + 1}.html">next</a></li>' if page < pages else ""
    return f'<html><body><ol class="row">{items}</ol><ul class="pager">{nxt}</ul></body></html>'


def test_books_story(httpserver):
    httpserver.expect_request("/").respond_with_data(_catalogue(1), content_type="text/html")
    httpserver.expect_request("/catalogue/page-2.html").respond_with_data(_catalogue(2), content_type="text/html")
    for i in range(1, 7):
        httpserver.expect_request(f"/book/{i}").respond_with_data(BOOK.format(title=f"Book {i}", desc=f"About book {i}.", upc=f"upc{i}", price=f"{i}.00", n=i), content_type="text/html")
    plan = (
        wq.reference(httpserver.url_for("/")).resolve()
        .paginate(next="li.next a", max_pages=5)
        .select_all("ol.row li")
        .extract(
            title=wq.doc.select("h3 a").attr("title"),
            price=wq.doc.select("p.price_color").attr("text"),
            rating=wq.doc.select("p.star-rating").attr("class"),
            detail=wq.doc.select("h3 a").attr("href").resolve().extract(
                description=wq.doc.select("#product_description ~ p").attr("text"),
                info=wq.doc.select_all("table tr").extract(wq.doc.select("td").attr("text").alias(wq.doc.select("th").attr("text"))).merge(),
            ).project(),
        )
        .filter(wq.doc.field("price") != "£4.00")
        .project()
    )
    with WebClient() as wc:
        rows = wc.execute(plan)
    assert [r["title"] for r in rows] == ["Book 1", "Book 2", "Book 3", "Book 5", "Book 6"]  # 2 pages, £4.00 filtered out
    assert rows[0]["detail"] == {"description": "About book 1.", "info": {"UPC": "upc1", "Price (incl. tax)": "£1.00", "Availability": "In stock (1 available)"}}
    assert rows[0]["rating"] == "star-rating Three"
