"""The lab (roadmap N13): every fixture publishes its expected result, and the client finds
exactly that -- tests, demos and docs assert against one set of facts.

The contract is ``/lab/index.json`` (name, title, path, feature, browser) + ``/lab/<name>.json``
(the expected result, with the paths and selectors the test needs). Two sites honour it: the
in-process ``webclient.lab`` (the default here) and the product website (``LAB_URL=http://…``
runs this same file against it -- the website IS the lab)."""

import json

from webclient.interface import wq
import os

import pytest

from webclient import RETURN, WebClient, WebException
from webclient.lab import FIXTURES, LabServer


@pytest.fixture(scope="module")
def lab():
    url = os.environ.get("LAB_URL")
    if url:
        yield url.rstrip("/")
        return
    with LabServer() as srv:
        yield srv.base


@pytest.fixture(scope="module")
def wc():
    with WebClient(timeout=15.0) as client:
        yield client


@pytest.fixture(scope="module")
def index(lab, wc):
    """name -> fixture entry (its ``path`` is where the page lives on THIS site)."""
    listed = json.loads(wc.fetch(f"{lab}/lab/index.json").content)
    return {f["name"]: f for f in listed}


def expected(lab, wc, name):
    return json.loads(wc.fetch(f"{lab}/lab/{name}.json").content)


def test_index_lists_every_fixture_with_an_expected_result(lab, wc, index):
    if not os.environ.get("LAB_URL"):
        assert set(index) == set(FIXTURES)
    assert set(index) >= set(FIXTURES), "the website must cover every in-process fixture"
    for name in index:
        assert isinstance(expected(lab, wc, name), dict)
    landing = wc.fetch(f"{lab}/")
    assert landing.title and any(l.url.rstrip("/").endswith("/lab") for l in landing.links())


def test_shop_records_links_and_patterns(lab, wc, index):
    exp = expected(lab, wc, "shop")
    doc = wc.fetch(f"{lab}{index['shop']['path']}")
    cards = doc.select_all(exp["record_selector"])
    assert len(cards) == exp["records"]
    assert [c.select(".title").attr("text") for c in cards] == exp["titles"]
    assert [c.select(".price").attr("data-price") for c in cards] == exp["prices"]
    assert doc.patterns(for_="extract")[0].subject == exp["record_selector"]
    assert [f.name for f in doc.flags()] == exp["flags"] and doc.transport().final_tier == exp["tier"]
    paths = [l.url.replace(lab, "") for l in doc.links()]
    assert sorted(set(paths)) == sorted(set(exp["links"]))
    item = wc.fetch(f"{lab}{exp['item']}")
    assert item.kind == "json" and item.select("stock.count").attr("value") == exp["item_stock"]


def test_spa_feed_static_signals(lab, wc, index):
    exp = expected(lab, wc, "spa")
    shell = wc.fetch(f"{lab}{index['spa']['path']}", browser=False)
    assert [f.name for f in shell.flags()] == exp["static_flags"] and shell.spa().remedy == exp["remedy"]
    assert len(shell.select_all(exp["record_selector"])) == exp["records_static"]


def test_pagination_rel_next_and_cursor(lab, wc, index):
    exp = expected(lab, wc, "paginated")
    first = wc.fetch(f"{lab}{index['paginated']['path']}")
    assert "pagination" in [f.name for f in first.flags()]
    assert first.next_link().url.replace(lab, "") == exp["next_of_1"]
    pages = first.paginate(next=wq.doc.next_link(), max_pages=10)
    total = sum(len(p.select_all(exp["record_selector"])) for p in pages)
    assert len(pages) == exp["pages"] and total == exp["total"]
    cur = expected(lab, wc, "cursor")
    api = wc.fetch(f"{lab}{index['cursor']['path']}")
    pages = api.paginate(cursor=cur["cursor_path"], param="after", max_pages=10)
    ids = [i for p in pages for i in json.loads(p.content)["items"]]
    assert len(ids) == cur["total"]


def test_tabs_forms_login_antibot_signals(lab, wc, index):
    tx = expected(lab, wc, "tabs")
    tabs = wc.fetch(f"{lab}{index['tabs']['path']}")
    assert "tabbed" in [f.name for f in tabs.flags()] and len(tabs.select_all(tx["record_selector"])) == tx["records"]
    forms = wc.fetch(f"{lab}{index['forms']['path']}")
    fx = expected(lab, wc, "forms")
    assert set(fx["flags"]) <= {f.name for f in forms.flags()}
    assert sorted(forms.forms().value[0].field_names) == sorted(fx["form_fields"])
    login = wc.fetch(f"{lab}{index['login']['path']}")
    lx = expected(lab, wc, "login")
    assert {f.name for f in login.flags()} >= {"login_present", "login_required"}
    with pytest.raises(WebException) as info:
        wc.fetch(f"{lab}{index['login']['path']}", browser="auto")
    assert info.value.error.code == lx["auto_error"]
    bot = wc.fetch(f"{lab}{index['antibot']['path']}", optional=True)
    bx = expected(lab, wc, "antibot")
    assert bot.status_code == bx["status"] and bot.anti_bot_triggered().present
    assert bot.anti_bot_triggered().remedy == bx["remedy"]


def test_redirects_errors_and_kinds(lab, wc, index):
    rx = expected(lab, wc, "redirect")
    doc = wc.fetch(f"{lab}{index['redirect']['path']}")
    assert doc.final_url.replace(lab, "") == rx["final"] and doc.title == rx["title"]
    assert len(doc.events_of("network")) == rx["hops"] + 1
    ex = expected(lab, wc, "errors")
    for code, expected_code in ex["codes"].items():
        d = wc.fetch(f"{lab}{ex['base']}/{code}", error=RETURN)
        assert d.error.code == expected_code and d.error.retriable is ex["retriable"][code]
    api = expected(lab, wc, "api")
    j = wc.fetch(f"{lab}{index['api']['path']}")
    assert j.kind == api["kind"] and len(j.select_all(api["path"])) == api["count"]
    assert j.select_all(api["path"])[0].select("name").attr("value") == api["first_name"]
    rss = wc.fetch(f"{lab}{index['rss']['path']}")
    rx2 = expected(lab, wc, "rss")
    assert rss.kind == rx2["kind"] and len(rss.select_all("item")) == rx2["items"]
    assert rss.select("item").select("title").attr("text") == rx2["first_title"]
    pdf = wc.fetch(f"{lab}{index['pdf']['path']}")
    assert pdf.kind == expected(lab, wc, "pdf")["kind"] and pdf.content.startswith(b"%PDF")
    large = wc.fetch(f"{lab}{index['large']['path']}")
    lx = expected(lab, wc, "large")
    assert len(large.select_all(lx["record_selector"])) == lx["records"] and large.large_document().present
    assert wc.fetch(f"{lab}{index['gzip']['path']}").title == expected(lab, wc, "gzip")["title"]


def test_sitemap_robots_and_a_crawl(lab, wc, index):
    sx = expected(lab, wc, "sitemap")
    seed = f"{lab}{sx['seed']}"
    urls = [r.url.replace(lab, "") for r in wc.sitemap(seed)]
    assert urls == sx["sitemap_urls"]
    robots = wc.robots(seed)
    assert robots.exists and robots.sitemaps and not robots.allowed(f"{lab}{sx['must_skip']}")
    with wc.crawl(seed, auto=True, max_pages=sx["crawl_pages"], browser=False) as crawl:
        crawl.run()
    got = {p.final_url.replace(lab, "") for p in crawl.pages}
    assert sx["seed"] in got and sx["must_reach"] in got and sx["must_skip"] not in got  # robots
    assert any(f.reason == "robots-disallowed" for f in crawl.failures)


def test_interacted_pagination_loads_all_records(lab, wc):
    # click=: drive a JS "load more" button (an interacted pager) until the list is exhausted,
    # then extract EVERY record from the one fully-loaded page (append / exhaust-then-extract).
    from webclient.interface import wq

    exp = expected(lab, wc, "loadmore")
    live = wc.ref(f"{lab}/lab/loadmore").resolve(browser=True).collect()
    assert len(live.select_all("li.item")) == exp["initial"]  # 3 to start
    pages = list(live.paginate(click="#more", records="li.item", max_pages=10))
    assert len(pages) == 1  # append mode -> one fully-loaded page
    assert len(pages[0].select_all("li.item")) == exp["total"]  # all 12 loaded
    wc.release(live)


@pytest.mark.parametrize("name", ["spa", "feed", "app"])
def test_browser_fixtures(lab, wc, index, name):
    exp = expected(lab, wc, name)
    path = index[name]["path"]
    if name == "spa":
        doc = wc.fetch(f"{lab}{path}", browser="auto")
        assert doc.transport().escalation == exp["tiers_auto"]
        assert len(doc.select_all(exp["record_selector"])) == exp["records_rendered"]
    elif name == "feed":
        doc = wc.fetch(f"{lab}{path}", browser="always")
        assert len(doc.select_all(exp["record_selector"])) == exp["records_rendered"]
        assert sorted({x.url.replace(lab, "") for x in doc.xhr_endpoints()}) == exp["xhr_endpoints"]
    else:
        live = wc.ref(f"{lab}{path}").resolve(browser=True).collect()
        assert {c.selector for c in live.controls()} >= set(exp["controls"])
        rows = exp["rows"]
        live.write("#qty", "7").click("#add").wait_for(rows)
        assert len(live.select_all(rows)) == exp["after_add_rows"]
        live.click("#load").wait_for(f"{rows}:nth-child({exp['after_load_rows']})")
        assert len(live.select_all(rows)) == exp["after_load_rows"]
        wc.release(live)
