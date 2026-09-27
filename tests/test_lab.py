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


def test_split_sections_table_and_structured_metadata(lab, wc, index):
    # a dataset split across two differently-shaped sections: each is its own simple query.
    sx = expected(lab, wc, "sections")
    sec = wc.fetch(f"{lab}{index['sections']['path']}")
    up = sec.select_all(sx["upcoming_selector"])
    past = sec.select_all(sx["archived_selector"])
    assert len(up) == sx["upcoming"] and len(past) == sx["archived"]
    assert up[0].select(".what").attr("text") == sx["upcoming_title"]

    # a data table: rows/columns, and a GFM table in the markdown render.
    tx = expected(lab, wc, "table")
    tbl = wc.fetch(f"{lab}{index['table']['path']}")
    rows = tbl.select_all(tx["row_selector"])
    assert len(rows) == tx["rows"]
    assert [c.attr("text") for c in rows[0].select_all("td")] == tx["first_row"]
    assert all(col in tbl.markdown() for col in tx["columns"])

    # structured metadata: JSON-LD type, Open Graph keys, canonical, and the feed link.
    mx = expected(lab, wc, "structured")
    meta = wc.fetch(f"{lab}{index['structured']['path']}").metadata()
    assert meta.schema_types == mx["schema_types"] and meta.page_type == mx["page_type"]
    assert meta.og_keys == mx["og_keys"]
    assert meta.canonical_url.replace(lab, "") == mx["canonical"]
    assert [f.replace(lab, "") for f in meta.feeds] == [mx["feed"]]


def test_consent_wall_and_token_auth(lab, wc, index):
    # a consent wall: cookie_banner fires from the served HTML (static tier), remedy is a browser render.
    cx = expected(lab, wc, "consent")
    doc = wc.fetch(f"{lab}{index['consent']['path']}", browser=False)
    assert doc.cookie_banner().present and doc.cookie_banner().remedy == cx["remedy"]
    assert len(doc.select_all(cx["record_selector"])) == cx["records"]

    # a bearer-token API: 401 without the header, the records with it.
    tx = expected(lab, wc, "token")
    url = f"{lab}{tx['data']}"
    denied = wc.fetch(url, error=RETURN)
    assert denied.error.code == "fetch.http_status"
    ok = wc.fetch(url, headers={tx["header"]: f"{tx['scheme']} {tx['token']}"})
    assert ok.kind == "json" and len(ok.select_all("items")) == tx["items"]


def test_board_ordered_filtered_live_signals(lab, wc, index):
    # the pagination-shape signals: the listing is date-sorted newest-first (ordered), timely
    # (live, so it drifts while you page), and has sort + facet controls (ordered / filtered).
    bx = expected(lab, wc, "board")
    doc = wc.fetch(f"{lab}{index['board']['path']}")
    assert len(doc.select_all(bx["record_selector"])) == bx["records"]
    # the pagination-shape flags are read via their own accessors (not the flags() digest).
    assert all(getattr(doc, name)().present for name in bx["present"])
    assert doc.ordered().value.direction == bx["order_direction"]


def test_news_record_spans_two_sibling_rows(lab, wc, index):
    # rebuilt from the LIVE Hacker News front page: each story is TWO adjacent sibling <tr> rows
    # (title in tr.athing, then points/user/age in the next tr.subtext). A record selector that
    # points at the title row does NOT contain the subtext fields -- they are reached with an
    # adjacent-sibling hop (CSS ":scope + tr.subtext ..." or XPath "./following-sibling::tr[1]").
    nx = expected(lab, wc, "news")
    doc = wc.fetch(f"{lab}{index['news']['path']}")
    athings = doc.select_all(nx["record_selector"])
    assert len(athings) == nx["records"]
    # the subtext fields are NOT inside the athing record subtree (the naive read is empty)
    assert athings[0].select(".score", optional=True, error=RETURN).attr("text", optional=True) in (None, "")
    # the correct extraction: a following-sibling hop reaches the paired subtext row per field
    rows = wq.doc.select_all("tr.athing").extract(
        title=wq.doc.select(".titleline a").attr("text"),
        points=wq.doc.select(f'{nx["subtext_hop"]}//span[@class="score"]').attr("text", r"(\d+)", group=1),
        user=wq.doc.select(f'{nx["subtext_hop"]}//a[@class="hnuser"]').attr("text"),
        age=wq.doc.select(f'{nx["subtext_hop"]}//span[@class="age"]').attr(nx["timestamp_attr"]),
    ).project().collect(doc)
    assert len(rows) == nx["records"]
    assert [f in rows[0] for f in nx["fields"]] == [True] * len(nx["fields"])
    assert rows[0]["points"] == "412" and rows[0]["user"] == "hexdump"  # cleaned + from the sibling row
    assert rows[0]["age"].startswith("20") and "T" in rows[0]["age"]     # the ISO timestamp, from the attr


def test_quotes_record_has_a_list_valued_field(lab, wc, index):
    # rebuilt from quotes.toscrape.com: a field that is a LIST (a quote's many tags), not a scalar.
    # extract handles a many-valued column -- captured as a nested select_all, or by splitting the
    # mirrored <meta class="keywords" content="a,b,c"> attribute.
    qx = expected(lab, wc, "quotes")
    doc = wc.fetch(f"{lab}{index['quotes']['path']}")
    quotes = doc.select_all(qx["record_selector"])
    assert len(quotes) == qx["records"]
    rows = wq.doc.select_all(qx["record_selector"]).extract(
        text=wq.doc.select(".text").attr("text"),
        author=wq.doc.select(".author").attr("text"),
        tags=wq.doc.select_all(qx["tags_selector"]).attr("text"),
    ).project().collect(doc)
    assert isinstance(rows[0]["tags"], list) and rows[0]["tags"] == qx["first_tags"]
    # the same list is mirrored in the meta keywords attribute (comma-joined) -- an alternate path
    kw = quotes[0].select("meta.keywords").attr(qx["keywords_attr"])
    assert kw.split(",") == qx["first_tags"]


def test_catalog_value_in_a_class_token_and_a_title_attr(lab, wc, index):
    # rebuilt from books.toscrape.com: the star rating is the SECOND class token
    # (class="star-rating Three"), and the full title is in the anchor's title attribute while the
    # visible text is truncated. Both defeat a naive .attr("text").
    cx = expected(lab, wc, "catalog")
    doc = wc.fetch(f"{lab}{index['catalog']['path']}")
    pods = doc.select_all(cx["record_selector"])
    assert len(pods) == cx["records"]
    rows = wq.doc.select_all(cx["record_selector"]).extract(
        title=wq.doc.select("h3 a").attr(cx["title_attr"]),
        rating=wq.doc.select("p.star-rating").attr("class", r"star-rating\s+(\w+)", group=1),
        price=wq.doc.select(cx["price_selector"]).attr("text"),
    ).project().collect(doc)
    assert rows[0]["rating"] == cx["first_rating"]          # the token pulled out of the class string
    assert rows[0]["title"] == "A Light in the Attic" and rows[0]["price"].startswith("£")
    # a LONG title: the attr holds the full value while the visible link TEXT is truncated with an
    # ellipsis -- proving the attribute (not .attr("text")) was the right source.
    sapiens = "Sapiens: A Brief History of Humankind"
    idx = next(i for i, r in enumerate(rows) if r["title"] == sapiens)
    assert "…" not in rows[idx]["title"]                    # full title, from the attr
    assert pods[idx].select("h3 a").attr("text").endswith("…")  # visible text truncated


def test_store_filter_drops_sold_out_rows(lab, wc, index):
    # a listing where only SOME rows carry a sold-out badge: an "in-stock only" answer needs a
    # .filter() (a row-level predicate), not just a selector -- the sold-out rows are dropped.
    sx = expected(lab, wc, "store")
    doc = wc.fetch(f"{lab}{index['store']['path']}")
    assert len(doc.select_all(sx["record_selector"])) == sx["records"]  # every row is present in the HTML
    rows = wq.doc.select_all(sx["record_selector"]).filter(
        ~wq.doc.select(sx["sold_out_selector"], optional=True).is_ok()
    ).extract(name=wq.doc.select(".name").attr("text")).project().collect(doc)
    assert [r["name"] for r in rows] == sx["in_stock_names"]  # only the in-stock rows, in order
    assert len(rows) == sx["in_stock"] and len(rows) < sx["records"]  # the filter genuinely dropped rows


def test_overlap_pages_dedup_with_distinct(lab, wc, index):
    # consecutive pages share two items and a "Sponsored" row rides every page: a plain paginated walk
    # yields duplicate records; the record-level distinct on project drops them to the unique set.
    ox = expected(lab, wc, "overlap")
    from webclient.pipelines.onboarding.query_build import _executable_query

    expr = wq.doc.select_all(ox["record_selector"]).extract(name=wq.doc.select(".name").attr("text")).project()
    url = f"{lab}{index['overlap']['path']}"
    dupey = _executable_query(expr, url, None, paginate=False).collect()  # page one only (a reference point)
    deduped = _executable_query(expr, url, None, paginate=True, max_pages=5).collect()  # paginated -> distinct baked
    names = [r["name"] for r in deduped]
    assert names.count(ox["sticky"]) == 1                          # the sticky row appears once, not per page
    assert len(names) == len(set(names)) == ox["unique"]           # every record once (8 items + 1 sponsored)
    assert len(dupey) < len(deduped)                               # page one alone had fewer than the full union


def test_looppager_falls_back_to_the_page_param(lab, wc, index):
    # the '»' Next link loops back to page one, but the ?page_num= param walks all pages: the pager
    # probe rejects the looping next (a repeat) and the page-param mode carries the walk.
    lx = expected(lab, wc, "looppager")
    from webclient.pipelines.onboarding.query import _confirmed_mode, _DEFAULT_NEXT
    from webclient.pipelines.onboarding.query_build import _executable_query

    doc = wc.fetch(f"{lab}{index['looppager']['path']}")
    hint = doc.pagination().value
    assert any(m.mode == "pages" and m.param == lx["param"] for m in hint.modes)   # ?page_num= is recognised
    cm = _confirmed_mode(doc, hint, lx["record_selector"])
    assert cm not in (None, _DEFAULT_NEXT) and cm.mode == "pages"                   # the working pager, not the loop
    expr = wq.doc.select_all(lx["record_selector"]).extract(name=wq.doc.select(".name").attr("text")).project()
    rows = _executable_query(expr, f"{lab}{index['looppager']['path']}", None, paginate=True, mode=cm,
                             max_pages=10).collect()
    assert len(rows) == lx["total"] and len({r["name"] for r in rows}) == lx["total"]  # all pages, no loop


def test_deep_paginate_plus_per_record_resolve(lab, wc, index):
    # the deepest shape: the dataset spans PAGES, and a required field (SKU) is only on each item's
    # OWN detail page -- so a correct query must BOTH walk the pagination AND, per record, follow the
    # item's link and .resolve() it to read the field.
    dx = expected(lab, wc, "deep")
    rows = (
        wq.reference(f"{lab}{index['deep']['path']}").resolve()
        .paginate(next=wq.doc.next_link(), records=dx["record_selector"], max_pages=10)
        .select_all(dx["record_selector"]).extract(
            name=wq.doc.select(".name").attr("text"),
            sku=wq.doc.select(dx["detail_link"]).attr("href").resolve().select(".sku").attr("text"),
        ).project().collect()
    )
    assert len(rows) == dx["total"] and len({r["name"] for r in rows}) == dx["total"]  # all pages, no dupes
    assert rows[0]["sku"] == dx["sku_of_1"]                                             # from the detail page
    assert all(r["sku"] == f"SKU-{i + 1}" for i, r in enumerate(rows))                  # each record's own detail


def test_crawl_mini_site_scopes_and_dedups(lab, wc, index):
    # a small linked site with cross-links, cycles and one off-site link: every in-scope page is
    # reached exactly once (dedup), and the off-site link stays out of scope (same_origin).
    sx = expected(lab, wc, "site")
    with wc.crawl(f"{lab}{sx['seed']}", auto=True, max_pages=40, depth=5, browser=False, obey_robots=False) as cr:
        cr.run()
    got = {p.final_url.replace(lab, "") for p in cr.pages}
    on_site = [u for u in got if u == "/lab/site" or u.startswith("/lab/site/")]
    assert set(sx["pages"]) <= got
    assert not any("example.com" in u for u in got)
    assert len(on_site) == sx["count"]


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


def test_job_board_nested_browser_resolve_reads_the_embed_and_shows_its_api(lab, wc, tmp_path):
    # the shape of a careers site on a hosted board: the listing is rendered from a JSON API, each
    # posting lives in a CROSS-origin embed rendered late. A nested browser="auto" resolve must wait
    # for it and capture it; the trace shows which step held which page, and the listing's network
    # view names the API (and the field) its titles came from.
    from webclient.interface import wq
    from webclient.trace import read

    exp = expected(lab, wc, "jobs")
    plan = (
        wq.reference(f"{lab}/lab/jobs").resolve(browser="auto")
        .select_all("li.job")
        .extract(title=wq.doc.select("a").attr("text"),
                 detail=wq.doc.select("a").attr("href").resolve(browser="auto").extract(post=wq.doc.select("[data-wc-frame] h2").attr("text"), text=wq.doc.text()).project())
        .project()
    )
    with wc.trace(tmp_path / "jobs.jsonl"):
        rows = wc.execute(plan)
    assert [r["title"] for r in rows] == [j["title"] for j in exp["jobs"]]
    assert all(f"EMBED-DESCRIPTION-{j['id']}" in r["detail"]["text"] for r, j in zip(rows, exp["jobs"]))
    evs = read(tmp_path / "jobs.jsonl").events
    leased = [e for e in evs if e.topic == "resource" and (e.detail or {}).get("what") == "leased" and e.detail.get("kind") == "page"]
    assert len(leased) >= 1 + len(exp["jobs"]) and any("kw:detail" in (e.step or "") for e in leased)
    views = [e for e in evs if e.topic == "network.view"]
    listing = next(v for v in views if v.detail["url"].endswith("/lab/jobs"))
    api = next(r for r in listing.detail["requests"] if r["url"].endswith("/lab/jobs/api"))
    fields = {n.get("field") for n in api["produced"]}
    assert api["matched"] >= len(exp["jobs"]) and {f"jobs[{i}].title" for i in range(len(exp["jobs"]))} <= fields
