"""P3 gate: one evaluator -- eager and lazy agree, rows, policies, fan-out."""
import asyncio

import pytest

from webclient import RAISE, RETURN, Collection, Field, Reference, WebClient, doc, ref, reference, when
from webclient.executor import fan_out

CARDS = """
<html><body>
  <div class="card"><span class="title">Aeropress</span>
    <span class="status">Active</span><a href="/i/1?ref=home">go</a></div>
  <div class="card"><span class="title">Grinder</span>
    <span class="status">Inactive</span><a href="/i/2?ref=home">go</a></div>
  <div class="card"><span class="title">Kettle</span>
    <span class="status">Active</span><a href="/i/3?ref=home">go</a></div>
</body></html>
"""


@pytest.fixture
def wc():
    with WebClient() as client:
        yield client


@pytest.fixture
def site(httpserver, wc):
    httpserver.expect_request("/cards").respond_with_data(
        CARDS, content_type="text/html")
    for n in (1, 2, 3):
        httpserver.expect_request(f"/i/{n}").respond_with_json(
            {"id": n, "detail": f"detail-{n}"})
    return httpserver


def rows_of(wc, site, expr):
    return wc.execute(expr, wc.ref(site.url_for("/cards")))


def test_scalar_plan_against_a_context(site, wc):
    page = wc.ref(site.url_for("/cards")).resolve().collect()
    got = wc.execute(doc.select(".title").attr("text"), page)
    assert isinstance(got, Field) and got.get() == "Aeropress"


def test_rooted_plan_needs_no_context(site, wc):
    expr = reference(site.url_for("/cards")).resolve().select(".title").attr("text")
    assert wc.execute(expr).get() == "Aeropress"
    with pytest.raises(ValueError, match="needs a context"):
        wc.execute(doc.select(".title"))


def test_collect_is_the_lazy_trigger(site, wc):
    # PLAN §8 stage: .collect() runs a rooted plan (via the process default
    # client) -- the single evaluation trigger, additive to wc.execute.
    expr = reference(site.url_for("/cards")).resolve().select(".title").attr("text")
    assert expr.collect().get() == "Aeropress"
    # equivalent to executing it explicitly
    assert expr.collect().get() == wc.execute(expr).get()


def test_free_when_and_filter(site, wc):
    # PLAN §8: Polars-style free when(cond).then(a).otherwise(b), and free
    # filter(coll, pred) == coll.filter(pred). Usable now on the lazy roots.
    from webclient import filter as lazy_filter
    from webclient import when
    ctx = wc.ref(site.url_for("/cards"))
    rows = wc.execute(
        ref.resolve().select_all(".card").extract(
            title=doc.select(".title").attr("text"),
            state=when(doc.select(".status").attr("text") == "Active")
            .then("on").otherwise("off")).project(),
        ctx)
    assert [(r["title"], r["state"]) for r in rows] == [
        ("Aeropress", "on"), ("Grinder", "off"), ("Kettle", "on")]

    kept = lazy_filter(
        ref.resolve().select_all(".card"),
        doc.select(".status").attr("text") == "Active").extract(
        title=doc.select(".title").attr("text")).project()
    assert [r["title"] for r in wc.execute(kept, ctx)] == ["Aeropress", "Kettle"]


def test_client_bound_lazy_root(site, wc):
    # wc.lazy(url): a lazy root bound to THIS client; collect() runs on its core
    # (not the process default). The companion to Expr.collect() (PLAN §8).
    rows = (wc.lazy(site.url_for("/cards")).resolve().select_all(".card")
            .extract(title=doc.select(".title").attr("text")).project().collect())
    assert [r["title"] for r in rows] == ["Aeropress", "Grinder", "Kettle"]
    one = wc.lazy(site.url_for("/cards")).resolve().select(".title").attr("text")
    assert one.collect().get() == "Aeropress"


def test_extract_rows_and_project(site, wc):
    rows = rows_of(wc, site, ref.resolve().select_all(".card").extract(
        title=doc.select(".title").attr("text"),
        active=doc.select(".status").attr("text") == "Active",
    ).project())
    assert {r["title"]: r["active"] for r in rows} == {
        "Aeropress": True, "Grinder": False, "Kettle": True}


def test_filter_on_expression_and_on_extracted_field(site, wc):
    base = ref.resolve().select_all(".card").extract(
        title=doc.select(".title").attr("text"),
        active=doc.select(".status").attr("text") == "Active")
    by_expr = rows_of(wc, site, base.filter(doc.select(".status").attr("text") == "Active").project())
    by_field = rows_of(wc, site, base.filter(doc.field("active")).project())
    assert sorted(r["title"] for r in by_expr) == ["Aeropress", "Kettle"]
    assert by_field == by_expr


def test_follow_links_via_reference_and_param(site, wc):
    rows = rows_of(wc, site, ref.resolve().select_all(".card").extract(
        title=doc.select(".title").attr("text"),
        link=doc.select("a").attr("href"),
    ).extract(
        detail=doc.reference("link").resolve().select("detail").attr("value"),
    ).project())
    assert {r["title"]: r["detail"] for r in rows} == {
        "Aeropress": "detail-1", "Grinder": "detail-2", "Kettle": "detail-3"}
    assert all(isinstance(r["link"], Reference) for r in rows)


def test_missing_field_is_not_ok_under_the_plan_default(site, wc):
    base = ref.resolve().select_all(".card").extract(
        title=doc.select(".title").attr("text"),
        oops=doc.select(".nope").attr("text"))
    rows = rows_of(wc, site, base.project())
    assert len(rows) == 3 and all(r["oops"] is None for r in rows)
    kept = rows_of(wc, site, base.filter(doc.field("oops").is_ok()).project())
    assert kept == []


def test_raise_policy_aborts_the_plan(site, wc):
    with pytest.raises(LookupError, match="nope"):
        rows_of(wc, site, ref.resolve().select_all(".card").extract(
            oops=doc.select(".nope", error=RAISE).attr("text")))


def test_when_then_otherwise_and_sibling_field(site, wc):
    status = doc.select(".status").attr("text")
    rows = rows_of(wc, site, ref.resolve().select_all(".card").extract(
        title=doc.select(".title").attr("text"),
        flag=when(status == "Active").then("on").otherwise("off"),
        again=doc.field("title"),
    ).project())
    assert [(r["flag"], r["again"] == r["title"]) for r in rows] == [
        ("on", True), ("off", True), ("on", True)]


def test_nested_collections_flatten(site, wc):
    expr = ref.resolve().select_all(".card").extract(
        links=doc.select_all("a")).documents("links").attr("href")
    got = rows_of(wc, site, expr)
    assert isinstance(got, Collection) and len(got) == 3
    assert all(isinstance(r, Reference) and r.ok for r in got)


def test_stream_yields_rows_and_publishes_plan_events(site, wc):
    phases = []
    wc.bus.subscribe("plan", lambda e: phases.append(e.phase))
    it = wc.execute(ref.resolve().select_all(".card").extract(
        title=doc.select(".title").attr("text")).project(),
        wc.ref(site.url_for("/cards")), stream=True)
    assert sorted(r["title"] for r in it) == ["Aeropress", "Grinder", "Kettle"]
    assert phases[0] == "started" and phases[-1] == "done"
    assert phases.count("row") == 3


def test_eager_and_lazy_agree(site, wc):
    page = wc.ref(site.url_for("/cards")).resolve().collect()
    cards = page.select_all(".card")
    cards.extract(title=doc.select(".title").attr("text"),
                  active=doc.select(".status").attr("text") == "Active")
    eager = cards.filter(doc.field("active")).project()
    lazy_rows = rows_of(wc, site, ref.resolve().select_all(".card").extract(
        title=doc.select(".title").attr("text"),
        active=doc.select(".status").attr("text") == "Active",
    ).filter(doc.field("active")).project())
    assert eager == lazy_rows
    assert cards.name.startswith("col:") and cards.root == page.name


def test_fan_out_is_bounded_and_ordered(wc):
    in_flight, peak = 0, 0

    async def work(i):
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.01)
        in_flight -= 1
        return i * 2

    results = wc._ensure_loop().run(fan_out(list(range(20)), work, limit=3))
    assert results == [i * 2 for i in range(20)] and peak == 3


def test_failing_row_cancels_siblings(wc):
    finished = []

    async def work(i):
        if i == 2:
            raise RuntimeError("row 2 blew up")
        await asyncio.sleep(0.05)
        finished.append(i)
        return i

    with pytest.raises(RuntimeError, match="row 2"):
        wc._ensure_loop().run(fan_out(list(range(6)), work, limit=6))
    assert len(finished) < 5           # siblings were cancelled, not drained


def test_per_call_collect_is_eager(site, wc):
    # op(..., _collect=True) records then collects immediately (one path).
    title = wc.ref(site.url_for("/cards")).resolve().select(
        ".title", _collect=True)                     # a materialised Document
    assert title.attr("text").get() == "Aeropress"   # eager from here
