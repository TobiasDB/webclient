"""Crawl: a stateful, client-held site traversal (context manager).

A small linked site is served locally (with a robots.txt), so the turn-based /
auto / keyword / robots behaviour is exercised over the real fetch+parse path.
"""

import pytest

from webclient import Crawl, Edge, WebClient

# a little site: home links to /a, /docs (keyword) and an external host; /a links
# to /b and /private (robots-disallowed); /docs links to /docs/pricing.
PAGES = {
    "/": '<a href="/a">Alpha</a> <a href="/docs">Pricing Docs</a>'
    ' <a href="https://external.example/x">Elsewhere</a>',
    "/a": '<a href="/b">Beta</a> <a href="/private">Secret</a>',
    "/b": '<a href="/a">back to Alpha</a>',
    "/docs": '<a href="/docs/pricing">Pricing plans</a>',
    "/docs/pricing": "Our pricing is simple.",
    "/private": "secret area",
}


@pytest.fixture
def wc():
    with WebClient() as client:
        yield client


@pytest.fixture
def site(httpserver):
    for path, body in PAGES.items():
        httpserver.expect_request(path).respond_with_data(
            f"<html><body>{body}</body></html>", content_type="text/html"
        )
    httpserver.expect_request("/robots.txt").respond_with_data(
        "User-agent: *\nDisallow: /private\n", content_type="text/plain"
    )
    return httpserver


def _urls(crawl: Crawl) -> list[str]:
    return [p.transport.final_url for p in crawl.pages if p.transport]


def test_auto_crawl_stays_same_origin(wc, site):
    with wc.crawl(site.url_for("/"), auto=True, max_pages=10) as crawl:
        crawl.run()
    urls = _urls(crawl)
    assert any(u.endswith("/a") for u in urls)  # followed same-origin links
    assert all("external.example" not in u for u in urls)  # not the external host
    assert crawl.done


def test_robots_disallow_is_honoured(wc, site):
    with wc.crawl(site.url_for("/"), auto=True, max_pages=20) as crawl:
        crawl.run()
    assert not any(u.endswith("/private") for u in _urls(crawl))


def test_robots_can_be_ignored(wc, site):
    with wc.crawl(site.url_for("/"), auto=True, max_pages=20, obey_robots=False) as crawl:
        crawl.run()
    assert any(u.endswith("/private") for u in _urls(crawl))


def test_turn_based_frontier_is_caller_driven(wc, site):
    with wc.crawl(site.url_for("/")) as crawl:
        crawl.step()  # fetch the seed only
        assert len(crawl.pages) == 1
        edges = {e.url for e in crawl.frontier}
        assert any(u.endswith("/a") for u in edges)  # discovered, not yet fetched
        assert all("external.example" not in u for u in edges)  # out of scope
        # the caller selects which edges to expand this round
        picks = [e for e in crawl.frontier if e.url.endswith("/a")]
        crawl.step(picks)
    assert len(crawl.pages) == 2


def test_keywords_drive_best_first(wc, site):
    # width=1 forces a choice each round; "pricing" should steer toward /docs.
    with wc.crawl(
        site.url_for("/"), auto=True, keywords=["pricing"], width=1, max_pages=3
    ) as crawl:
        crawl.run()
    assert any("pricing" in u.lower() for u in _urls(crawl))


def test_edges_carry_anchor_text(wc, site):
    with wc.crawl(site.url_for("/")) as crawl:
        crawl.step()
        docs = next(e for e in crawl.frontier if e.url.endswith("/docs"))
    assert "Pricing" in docs.text  # anchor text kept (the keyword signal)


def test_sitemap_is_an_eager_single_domain_crawl(wc, site):
    sm = wc.sitemap(site.url_for("/"), depth=3, width=20)
    assert isinstance(sm, Crawl) and sm.done
    assert len(sm.pages) >= 4  # mapped several pages of the one domain
    assert all("external.example" not in u for u in _urls(sm))


def test_crawl_chooses_which_facets_each_page_carries(wc, site):
    # ``facets`` restricts each page's summary to the named backings.
    with wc.crawl(
        site.url_for("/"), auto=True, max_pages=5, facets=["transport"]
    ) as crawl:
        crawl.run()
    assert crawl.pages  # crawled something
    assert all(p.transport is not None for p in crawl.pages)
    assert all(p.structure is None and p.metadata is None for p in crawl.pages)


def test_default_crawl_carries_only_the_lean_facets(wc, site):
    # no `facets` -> the lean DEFAULT_FACETS (transport + metadata), not every
    # facet, so a large crawl does not run structure/runtime/probe per page.
    from webclient.core.crawl.models import DEFAULT_FACETS

    assert DEFAULT_FACETS == ("transport", "metadata")
    with wc.crawl(site.url_for("/"), auto=True, max_pages=5) as crawl:
        crawl.run()
    assert crawl.pages
    assert all(p.transport is not None for p in crawl.pages)
    assert all(p.structure is None and p.runtime is None for p in crawl.pages)


def test_sitemaps_discovers_urls_from_robots_and_sitemap_xml(wc, httpserver):
    # robots.txt points at a sitemap index; the index points at a child sitemap
    # whose urlset lists the real pages.
    base = httpserver.url_for("/").rstrip("/")
    httpserver.expect_request("/robots.txt").respond_with_data(
        f"User-agent: *\nSitemap: {base}/sitemap_index.xml\n", content_type="text/plain"
    )
    httpserver.expect_request("/sitemap_index.xml").respond_with_data(
        '<?xml version="1.0"?>'
        '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        f"<sitemap><loc>{base}/pages.xml</loc></sitemap></sitemapindex>",
        content_type="application/xml",
    )
    httpserver.expect_request("/pages.xml").respond_with_data(
        '<?xml version="1.0"?>'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        f"<url><loc>{base}/a</loc></url><url><loc>{base}/b</loc></url></urlset>",
        content_type="application/xml",
    )
    refs = wc.sitemaps(httpserver.url_for("/"))
    urls = sorted(r.url for r in refs)
    assert urls == [f"{base}/a", f"{base}/b"]


def test_sitemaps_on_a_site_without_one_is_empty(wc, site):
    # the `site` fixture has a robots.txt with no Sitemap: and no /sitemap.xml.
    assert list(wc.sitemaps(site.url_for("/"))) == []


def test_context_manager_closes_the_crawl(wc, site):
    with wc.crawl(site.url_for("/")) as crawl:
        assert crawl.status == "running"
    assert crawl.status == "closed"  # aexit fired via the backing lifecycle
