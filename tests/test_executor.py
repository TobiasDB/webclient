import pytest

from webclient import OnError, WebClient, col, q

CARDS = """
<html><body>
  <div class="card"><span class="title">Aeropress</span>
    <span class="status">Active</span><a href="/i/1">go</a></div>
  <div class="card"><span class="title">Grinder</span>
    <span class="status">Inactive</span><a href="/i/2">go</a></div>
  <div class="card"><span class="title">Kettle</span>
    <span class="status">Active</span><a href="/i/3">go</a></div>
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


def test_scalar_plan_returns_single_value(site, wc):
    plan = q.ref.fetch().select(".title").text
    assert plan.collect(wc.ref(site.url_for("/cards"))) == "Aeropress"


def test_map_extracts_rows(site, wc):
    plan = q.ref.fetch().select_all(".card").map(
        title=q.node.select(".title").text,
        active=q.node.select(".status").text == "Active",
    )
    rows = plan.collect(wc.ref(site.url_for("/cards")))
    by_title = {r["title"]: r["active"] for r in rows}
    assert by_title == {"Aeropress": True, "Grinder": False, "Kettle": True}


def test_filter_drops_rows(site, wc):
    plan = (
        q.ref.fetch().select_all(".card")
        .map(title=q.node.select(".title").text,
             active=q.node.select(".status").text == "Active")
        .filter(col("active"))
    )
    rows = plan.collect(wc.ref(site.url_for("/cards")))
    assert sorted(r["title"] for r in rows) == ["Aeropress", "Kettle"]


def test_then_follows_links_via_col(site, wc):
    plan = (
        q.ref.fetch().select_all(".card")
        .map(title=q.node.select(".title").text,
             link=q.node.select("a").attr("href"))
        .then(detail=col("link").fetch().json.query("detail"))
    )
    rows = plan.collect(wc.ref(site.url_for("/cards")))
    by_title = {r["title"]: r["detail"] for r in rows}
    assert by_title == {"Aeropress": "detail-1", "Grinder": "detail-2",
                        "Kettle": "detail-3"}


def test_otherwise_skip_drops_failing_rows(site, wc):
    # .nope is missing -> select raises -> skip drops the row
    plan = q.ref.fetch().select_all(".card").map(
        title=q.node.select(".title").text,
        oops=q.node.select(".nope").text.otherwise(OnError.skip),
    )
    rows = plan.collect(wc.ref(site.url_for("/cards")))
    assert rows == []


def test_otherwise_ignore_yields_none(site, wc):
    plan = q.ref.fetch().select_all(".card").map(
        title=q.node.select(".title").text,
        oops=q.node.select(".nope").text.otherwise(OnError.ignore),
    )
    rows = plan.collect(wc.ref(site.url_for("/cards")))
    assert all(r["oops"] is None for r in rows)
    assert len(rows) == 3


def test_stream_yields_rows(site, wc):
    plan = q.ref.fetch().select_all(".card").map(
        title=q.node.select(".title").text)
    titles = sorted(r["title"] for r in plan.collect(
        wc.ref(site.url_for("/cards")), stream=True))
    assert titles == ["Aeropress", "Grinder", "Kettle"]


def test_execute_via_client_and_plan_events(site, wc):
    phases = []
    wc.bus.subscribe("plan", lambda e: phases.append(e.phase))
    plan = q.ref.fetch().select_all(".card").map(
        title=q.node.select(".title").text)
    rows = wc.execute(plan, wc.ref(site.url_for("/cards")))
    assert len(rows) == 3
    assert phases[0] == "started" and phases[-1] == "done"
    assert phases.count("row") == 3


def test_status_reports_stats(site, wc):
    plan = (
        q.ref.fetch().select_all(".card")
        .map(title=q.node.select(".title").text,
             link=q.node.select("a").attr("href"))
        .then(d=col("link").fetch().json.query("detail"))
    )
    wc.execute(plan, wc.ref(site.url_for("/cards")))
    graph_stats = list(wc._executor()._stats.values())[-1]
    assert graph_stats.rows == 3
    assert graph_stats.requests == 4      # 1 cards + 3 details
    assert graph_stats.done


def test_compile_tags_fetch_steps_as_http(wc):
    plan = q.ref.fetch().select_all(".card")
    graph = wc._executor().compile(plan.to_query())
    fetch_steps = [s for s in graph.steps if s.resource == "http"]
    assert len(fetch_steps) == 1
