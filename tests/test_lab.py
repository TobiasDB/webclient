"""The lab (roadmap N13): every fixture publishes its expected result, and the client finds
exactly that -- tests, demos and docs assert against one set of facts."""

import json

import pytest

from webclient import RETURN, WebClient, WebException
from webclient.lab import FIXTURES, LabServer, route


@pytest.fixture(scope="module")
def lab():
    with LabServer() as srv:
        yield srv.base


@pytest.fixture(scope="module")
def wc():
    with WebClient(timeout=15.0) as client:
        yield client


def expected(lab, name):
    status, headers, body = route("GET", f"/lab/{name}.json", {}, {}, b"")
    assert status == 200
    return json.loads(body)


def test_index_lists_every_fixture_with_an_expected_result(lab, wc):
    listed = json.loads(wc.fetch(f"{lab}/lab/index.json").content)
    assert {f["name"] for f in listed} == set(FIXTURES)
    for f in listed:
        assert isinstance(expected(lab, f["name"]), dict)
    landing = wc.fetch(f"{lab}/")
    assert landing.title == "webclient" and any(l.url.endswith("/lab") for l in landing.links())


def test_shop_records_links_and_patterns(lab, wc):
    exp = expected(lab, "shop")
    doc = wc.fetch(f"{lab}/lab/shop")
    cards = doc.select_all("div.card")
    assert len(cards) == exp["records"]
    assert [c.select(".title").attr("text") for c in cards] == exp["titles"]
    assert [c.select(".price").attr("data-price") for c in cards] == exp["prices"]
    assert doc.patterns(for_="extract")[0].subject == exp["record_selector"]
    assert [f.name for f in doc.flags()] == exp["flags"] and doc.transport().final_tier == exp["tier"]
    paths = [l.url.replace(lab, "") for l in doc.links()]
    assert sorted(paths) == sorted(exp["links"])
    item = wc.fetch(f"{lab}/lab/shop/items/2")
    assert item.kind == "json" and item.select("stock.count").attr("value") == 14


def test_spa_feed_static_signals(lab, wc):
    exp = expected(lab, "spa")
    shell = wc.fetch(f"{lab}/lab/spa", browser=False)
    assert [f.name for f in shell.flags()] == exp["static_flags"] and shell.spa().remedy == exp["remedy"]
    assert len(shell.select_all("li.item")) == exp["records_static"]


def test_pagination_rel_next_and_cursor(lab, wc):
    exp = expected(lab, "paginated")
    first = wc.fetch(f"{lab}/lab/paginated")
    assert "pagination" in [f.name for f in first.flags()]
    assert first.next_link().url.replace(lab, "") == exp["next_of_1"]
    pages = first.paginate(by="link", max_pages=10)
    total = sum(len(p.select_all(exp["record_selector"])) for p in pages)
    assert len(pages) == exp["pages"] and total == exp["total"]
    cur = expected(lab, "cursor")
    api = wc.fetch(f"{lab}/lab/cursor")
    pages = api.paginate(by="cursor", cursor=cur["cursor_path"], name="after", max_pages=10)
    ids = [i for p in pages for i in json.loads(p.content)["items"]]
    assert len(ids) == cur["total"]


def test_tabs_forms_login_antibot_signals(lab, wc):
    tabs = wc.fetch(f"{lab}/lab/tabs")
    assert "tabbed" in [f.name for f in tabs.flags()] and len(tabs.select_all("li.event")) == expected(lab, "tabs")["records"]
    forms = wc.fetch(f"{lab}/lab/forms")
    fx = expected(lab, "forms")
    assert set(fx["flags"]) <= {f.name for f in forms.flags()}
    assert sorted(forms.forms().value[0].field_names) == sorted(fx["form_fields"])
    login = wc.fetch(f"{lab}/lab/login")
    lx = expected(lab, "login")
    assert {f.name for f in login.flags()} >= {"login_present", "login_required"}
    with pytest.raises(WebException) as info:
        wc.fetch(f"{lab}/lab/login", browser="auto")
    assert info.value.error.code == lx["auto_error"]
    bot = wc.fetch(f"{lab}/lab/antibot", optional=True)
    bx = expected(lab, "antibot")
    assert bot.status_code == bx["status"] and bot.anti_bot_triggered().present
    assert bot.anti_bot_triggered().remedy == bx["remedy"]


def test_redirects_errors_and_kinds(lab, wc):
    rx = expected(lab, "redirect")
    doc = wc.fetch(f"{lab}/lab/redirect")
    assert doc.final_url.endswith(rx["final"]) and doc.title == rx["title"]
    assert len(doc.events_of("network")) == rx["hops"] + 1
    ex = expected(lab, "errors")
    for code, expected_code in ex["codes"].items():
        d = wc.fetch(f"{lab}/lab/errors/{code}", error=RETURN)
        assert d.error.code == expected_code and d.error.retriable is ex["retriable"][code]
    api = expected(lab, "api")
    j = wc.fetch(f"{lab}/lab/api")
    assert j.kind == api["kind"] and len(j.select_all(api["path"])) == api["count"]
    assert j.select_all(api["path"])[0].select("name").attr("value") == api["first_name"]
    rss = wc.fetch(f"{lab}/lab/rss")
    rx2 = expected(lab, "rss")
    assert rss.kind == rx2["kind"] and len(rss.select_all("item")) == rx2["items"]
    assert rss.select("item").select("title").attr("text") == rx2["first_title"]
    pdf = wc.fetch(f"{lab}/lab/pdf")
    assert pdf.kind == expected(lab, "pdf")["kind"] and pdf.content.startswith(b"%PDF")
    large = wc.fetch(f"{lab}/lab/large")
    lx = expected(lab, "large")
    assert len(large.select_all(lx["record_selector"])) == lx["records"] and large.large_document().present
    assert wc.fetch(f"{lab}/lab/gzip").title == expected(lab, "gzip")["title"]


def test_sitemap_robots_and_a_crawl(lab, wc):
    sx = expected(lab, "sitemap")
    urls = [r.url.replace(lab, "") for r in wc.sitemap(f"{lab}/lab/shop")]
    assert urls == sx["sitemap_urls"]
    robots = wc.robots(f"{lab}/lab/shop")
    assert robots.exists and robots.sitemaps and not robots.allowed(f"{lab}/lab/login")
    with wc.crawl(f"{lab}/lab/shop", auto=True, max_pages=6, browser=False) as crawl:
        crawl.run()
    got = {p.final_url.replace(lab, "") for p in crawl.pages}
    assert "/lab/shop" in got and "/lab/about" in got and "/lab/login" not in got  # robots
    assert any(f.reason == "robots-disallowed" for f in crawl.failures)


@pytest.mark.parametrize("name", ["spa", "feed", "app"])
def test_browser_fixtures(lab, wc, name):
    exp = expected(lab, name)
    if name == "spa":
        doc = wc.fetch(f"{lab}/lab/spa", browser="auto")
        assert doc.transport().escalation == exp["tiers_auto"]
        assert len(doc.select_all("li.item")) == exp["records_rendered"]
    elif name == "feed":
        doc = wc.fetch(f"{lab}/lab/feed", browser="always")
        assert len(doc.select_all("li.item")) == exp["records_rendered"]
        assert sorted({x.url.replace(lab, "") for x in doc.xhr_endpoints()}) == exp["xhr_endpoints"]
    else:
        live = wc.ref(f"{lab}/lab/app").resolve(browser=True).collect()
        assert {c.selector for c in live.controls()} >= set(exp["controls"])
        live.write("#qty", "7").click("#add").wait_for("#cart li")
        assert len(live.select_all("#cart li")) == exp["after_add_rows"]
        live.click("#load").wait_for("#cart li:nth-child(3)")
        assert len(live.select_all("#cart li")) == exp["after_load_rows"]
        wc.release(live)
