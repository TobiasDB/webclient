"""Value ops on a read: ``number()`` (the first number, or a number word) and ``map()``, with an
``attr`` pattern -- how a star rating coded in a class becomes a count."""

from webclient import WebClient
from webclient.interface import wq
from webclient.query.collection import Field

STARS = """<html><body><ol>
<li><p class="star-rating Three"><i></i><i></i><i></i><i></i><i></i></p><p class="price">£51.77</p><p class="stock">In stock (22 available)</p></li>
<li><p class="star-rating One"><i></i><i></i><i></i><i></i><i></i></p><p class="price">£1,234.00</p><p class="stock">Out of stock</p></li>
</ol></body></html>"""


def test_field_number_and_map():
    assert Field("£51.77").number().get() == 51.77
    assert Field("In stock (22 available)").number().get() == 22
    assert Field("1,234").number().get() == 1234
    assert Field("star-rating Three").number().get() == 3
    assert Field("none here").number(0).get() == 0
    assert Field("Three").map({"one": 1, "three": 3}).get() == 3
    assert Field("x").map({"one": 1}, -1).get() == -1


def test_star_rating_as_a_number_in_a_plan(httpserver):
    httpserver.expect_request("/").respond_with_data(STARS, content_type="text/html")
    d = wq.doc
    plan = (
        wq.reference(httpserver.url_for("/")).resolve().select_all("li")
        .extract(
            stars=d.select("p.star-rating").attr("class", r"star-rating (\w+)").number(),
            word=d.select("p.star-rating").attr("class", r"star-rating (\w+)"),
            price=d.select("p.price").attr("text").number(),
            available=d.select("p.stock").attr("text").number(0),
            icons=d.select("p.star-rating").attr("count"),
        )
        .project()
    )
    with WebClient() as wc:
        rows = wc.execute(plan)
    assert rows == [
        {"stars": 3, "word": "Three", "price": 51.77, "available": 22, "icons": 5},
        {"stars": 1, "word": "One", "price": 1234, "available": 0, "icons": 5},
    ]
