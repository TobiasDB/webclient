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


def test_field_date_and_datetime():
    import datetime as dt

    from webclient.query.collection import parse_when

    assert Field("2026-09-18").date().get() == "2026-09-18"
    assert Field("Posted 2026-09-18T14:05:00Z").datetime().get() == "2026-09-18T14:05:00+00:00"
    assert Field("18 Sep 2026").date().get() == "2026-09-18"
    assert Field("September 18th, 2026 at 2:05 pm").datetime().get() == "2026-09-18T14:05:00"
    assert Field("09/18/2026").date().get() == "2026-09-18"
    assert Field("18/09/2026").date(dayfirst=True).get() == "2026-09-18"
    assert Field("2026.09.18").date().get() == "2026-09-18"
    assert Field("18-09-26").date(dayfirst=True).get() == "2026-09-18"
    assert Field("20260918").date("%Y%m%d").get() == "2026-09-18"
    assert Field("no date").date(default="?").get() == "?"
    assert Field("In stock (22 available)").date().get() is None  # a stray number is not a date
    assert Field("Friday, 2026-Sep-18").date().get() == "2026-09-18"  # the dateutil fallback
    now = dt.datetime(2026, 9, 25, 12, 0, 0)
    assert parse_when("3 days ago", now=now) == dt.datetime(2026, 9, 22, 12, 0, 0)
    assert parse_when("yesterday", now=now).date() == dt.date(2026, 9, 24)
    assert parse_when("an hour ago", now=now) == dt.datetime(2026, 9, 25, 11, 0, 0)


def test_dates_in_a_plan(httpserver):
    httpserver.expect_request("/n").respond_with_data('<html><body><article><time datetime="2026-09-18T09:30:00Z">Sep 18</time><span class="d">18 Sep 2026</span></article></body></html>', content_type="text/html")
    d = wq.doc
    plan = wq.reference(httpserver.url_for("/n")).resolve().select_all("article").extract(
        when=d.select("time").attr("datetime").datetime(), day=d.select("span.d").attr("text").date()).project()
    with WebClient() as wc:
        assert wc.execute(plan) == [{"when": "2026-09-18T09:30:00+00:00", "day": "2026-09-18"}]


def test_field_split_to_a_collection_of_fields():
    parts = Field("a, b,, c").split(",")
    assert [p.get() for p in parts] == ["a", "b", "c"]
    assert [p.get() for p in Field("a  b\tc").split()] == ["a", "b", "c"]
    assert [p.get() for p in Field("1; 2 | 3").split(r"[;|]", regex=True)] == ["1", "2", "3"]
    assert [p.get() for p in Field("a,b,c").split(",", 1)] == ["a", "b,c"]
    assert [p.get() for p in Field("a,,b").split(",", keep_empty=True)] == ["a", "", "b"]
    assert len(Field(None).split(",")) == 0


TAGS = """<html><body><ul>
<li><b>Tea</b><i>green, hot , loose</i></li>
<li><b>Coffee</b><i>black</i></li>
</ul></body></html>"""


def test_split_in_a_plan_is_a_list_per_row(httpserver):
    httpserver.expect_request("/").respond_with_data(TAGS, content_type="text/html")
    d = wq.doc
    plan = (
        wq.reference(httpserver.url_for("/")).resolve().select_all("li")
        .extract(name=d.select("b").attr("text"), tags=d.select("i").attr("text").split(","))
        .project()
    )
    with WebClient() as wc:
        rows = wc.execute(plan)
    assert rows == [{"name": "Tea", "tags": ["green", "hot", "loose"]}, {"name": "Coffee", "tags": ["black"]}]
