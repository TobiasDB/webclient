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
    with wc.crawl(site.url_for("/"), auto=True, max_pages=10, browser=False) as crawl:
        crawl.run()
    urls = _urls(crawl)
    assert any(u.endswith("/a") for u in urls)  # followed same-origin links
    assert all("external.example" not in u for u in urls)  # not the external host
    assert crawl.done


def test_robots_disallow_is_honoured(wc, site):
    with wc.crawl(site.url_for("/"), auto=True, max_pages=20, browser=False) as crawl:
        crawl.run()
    assert not any(u.endswith("/private") for u in _urls(crawl))


def test_robots_can_be_ignored(wc, site):
    with wc.crawl(site.url_for("/"), auto=True, max_pages=20, obey_robots=False, browser=False) as crawl:
        crawl.run()
    assert any(u.endswith("/private") for u in _urls(crawl))


def test_turn_based_frontier_is_caller_driven(wc, site):
    with wc.crawl(site.url_for("/"), browser=False) as crawl:
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
        site.url_for("/"), auto=True, keywords=["pricing"], width=1, max_pages=3, browser=False
    ) as crawl:
        crawl.run()
    assert any("pricing" in u.lower() for u in _urls(crawl))


def test_edges_carry_anchor_text(wc, site):
    with wc.crawl(site.url_for("/"), browser=False) as crawl:
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
        site.url_for("/"), auto=True, max_pages=5, facets=["transport"], browser=False
    ) as crawl:
        crawl.run()
    assert crawl.pages  # crawled something
    assert all(p.transport is not None for p in crawl.pages)
    assert all(p.structure is None and p.metadata is None for p in crawl.pages)


def test_crawl_dedups_seed_variants(wc, site):
    # two seeds that canonicalise to the same target collapse to one edge.
    with wc.crawl(
        [site.url_for("/a"), site.url_for("/a/"), site.url_for("/a?utm_source=x")],
        max_pages=10, browser=False,
    ) as crawl:
        assert len(crawl.frontier) == 1  # deduped at seed time


def test_crawl_canonicalises_urls_for_dedup(wc, httpserver):
    # /page, /page/, and /page?utm_source=x are the same target -> fetched once.
    body = (
        '<a href="/page">a</a> <a href="/page/">b</a> '
        '<a href="/page?utm_source=nl">c</a> <a href="/page#frag">d</a>'
    )
    httpserver.expect_request("/").respond_with_data(
        f"<html><body>{body}</body></html>", content_type="text/html"
    )
    httpserver.expect_request("/page").respond_with_data(
        "<html><body>page</body></html>", content_type="text/html"
    )
    with wc.crawl(httpserver.url_for("/"), auto=True, max_pages=10, obey_robots=False, browser=False) as crawl:
        crawl.run()
    page_hits = [u for u in _urls(crawl) if u.rstrip("/").endswith("/page")]
    assert len(page_hits) == 1  # the four variants collapsed to one fetch


def test_facets_default_lives_on_the_model(wc, site):
    # the default facets are declared on the model (not applied deep in step): a
    # static crawl carries DEFAULT_FACETS; a browser crawl also gets `runtime`
    # (the model validator adds it) -- and that holds whether built bare or via
    # the client verb.
    from webclient.core.crawl.models import DEFAULT_FACETS, default_facets

    assert Crawl(browser=False).facets == list(DEFAULT_FACETS)
    assert Crawl(browser=True).facets == default_facets(browser=True)  # + runtime
    assert "runtime" in Crawl(browser=True).facets
    assert wc.crawl(site.url_for("/"), browser=False).facets == list(DEFAULT_FACETS)
    assert wc.crawl(site.url_for("/")).facets == default_facets(browser=True)


def test_crawl_defaults_to_auto_and_browser(wc, site):
    # the common case is "map this site": self-driving (auto) with a browser render
    # so JS links load. Both are on by default.
    crawl = wc.crawl(site.url_for("/"))
    assert crawl.auto is True and crawl.browser is True
    # opt-outs are honoured.
    static = wc.crawl(site.url_for("/"), auto=False, browser=False)
    assert static.auto is False and static.browser is False


def test_crawl_accepts_a_resolve_policy(wc, site):
    # a crawl can carry its own resiliency policy (retry/rate/proxy/anti-bot); it
    # is threaded into every fetch the crawl makes.
    from webclient import Resolve

    pol = Resolve.auto()
    crawl = wc.crawl(site.url_for("/"), browser=False, resolve=pol)
    assert crawl.resolve == pol
    with crawl:  # and a static crawl under a policy still runs offline
        crawl.step()
    assert crawl.pages


def test_crawl_prints_a_readable_digest(wc, site):
    with wc.crawl(site.url_for("/"), browser=False) as crawl:
        crawl.step()
        text = str(crawl)  # inspect mid-crawl (still running)
    assert "crawl [running]" in text and "page(s)" in text
    assert "pages:" in text and "[200]" in text
    assert "frontier (best first):" in text


def test_default_crawl_carries_the_decision_facets_not_browser_ones(wc, site):
    # no `facets` -> DEFAULT_FACETS: transport + metadata + structure (the "is it
    # ok / what is it / what's on it" facets an LLM maps a site with -- structure
    # is free since the crawl already parses each page to expand links). The
    # browser-only facets (runtime/probe) stay out -- they only apply after a
    # render, so on a static crawl they are empty anyway.
    from webclient.core.crawl.models import DEFAULT_FACETS

    assert DEFAULT_FACETS == ("transport", "metadata", "structure")
    with wc.crawl(site.url_for("/"), auto=True, max_pages=5, browser=False) as crawl:
        crawl.run()
    assert crawl.pages
    assert all(p.transport is not None for p in crawl.pages)
    assert any(p.structure is not None for p in crawl.pages)  # structure carried
    assert all(p.runtime is None and p.probe is None for p in crawl.pages)


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
    refs = wc.discover_sitemaps(httpserver.url_for("/"))
    urls = sorted(r.url for r in refs)
    assert urls == [f"{base}/a", f"{base}/b"]


def test_sitemaps_on_a_site_without_one_is_empty(wc, site):
    # the `site` fixture has a robots.txt with no Sitemap: and no /sitemap.xml.
    assert list(wc.discover_sitemaps(site.url_for("/"))) == []


def test_step_keeps_unfetched_edges_when_budget_is_nearly_full(wc, site):
    # L4: a step must not discard chosen edges it had no budget to fetch -- they
    # stay in the frontier for a later step.
    with wc.crawl(site.url_for("/"), auto=True, max_pages=2, browser=False) as crawl:
        crawl.step()                 # fetch the seed -> pages=1, discovers /a, /docs
        assert len(crawl.pages) == 1 and len(crawl.frontier) >= 2
        crawl.step()                 # room for only 1 more; the other edge survives
        assert len(crawl.pages) == 2 and len(crawl.frontier) >= 1


def test_context_manager_closes_the_crawl(wc, site):
    with wc.crawl(site.url_for("/"), browser=False) as crawl:
        assert crawl.status == "running"
    assert crawl.status == "closed"  # aexit fired via the backing lifecycle


# a page with the mix a real site has: a nav, an article "read more" link, a
# footer full of legal/social links, and asset links (a logo image, a stylesheet).
SCORED_PAGE = """
<html><body>
  <nav><a href="/products">Products</a><a href="/news">Newsroom</a></nav>
  <main>
    <article>
      <a href="/news/2026/09/big-announcement-today">Read more</a>
    </article>
  </main>
  <footer>
    <a href="/privacy">Privacy Policy</a>
    <a href="/terms">Terms of Use</a>
    <a href="https://twitter.com/acme">Follow us</a>
    <a href="/logo.png"><img src="/logo.png"></a>
    <a href="/style.css">theme</a>
  </footer>
</body></html>
"""


@pytest.fixture
def scored_site(httpserver):
    httpserver.expect_request("/").respond_with_data(
        SCORED_PAGE, content_type="text/html"
    )
    return httpserver


def test_frontier_drops_resource_links(wc, scored_site):
    # links to assets (a .png, a .css) are not crawlable pages -> filtered out.
    with wc.crawl(scored_site.url_for("/"), browser=False) as crawl:
        crawl.step()
    paths = [_path_of(e.url) for e in crawl.frontier]
    assert "/logo.png" not in paths and "/style.css" not in paths
    assert "/news" in paths  # real page links survive


def test_frontier_scores_and_sorts_useful_links_first(wc, scored_site):
    # the important links (article "read more", nav) must outrank the footer's
    # legal + social links, and the frontier is sorted by that score by default.
    with wc.crawl(scored_site.url_for("/"), browser=False) as crawl:
        crawl.step()
    by_path = {_path_of(e.url): e for e in crawl.frontier}
    read_more = by_path["/news/2026/09/big-announcement-today"]
    privacy = by_path["/privacy"]
    # the article link scores high; the legal footer link sinks below zero.
    assert read_more.score > 1.0
    assert privacy.score < 0
    # the frontier is sorted best-first, so the useful link leads.
    assert crawl.frontier[0].url == read_more.url
    # scores are monotonically non-increasing across the frontier.
    scores = [e.score for e in crawl.frontier]
    assert scores == sorted(scores, reverse=True)


def test_keyword_match_dominates_importance_score(wc, scored_site):
    # a keyword the caller passed must outrank the importance heuristic: even a
    # low-importance footer link that matches the keyword beats a high-importance
    # article link that does not (regression guard -- importance must not swamp
    # the explicit steering signal).
    from webclient.core.crawl.backing import CrawlBacking

    b = CrawlBacking()
    with wc.crawl(scored_site.url_for("/"), keywords=["privacy"], browser=False) as crawl:
        crawl.step()
        by_path = {_path_of(e.url): e for e in crawl.frontier}
        privacy = by_path["/privacy"]  # keyword match, footer (low importance)
        article = by_path["/news/2026/09/big-announcement-today"]  # high importance
        assert privacy.score < 0 < article.score  # importance disagrees...
        # ...but best-first selection puts the keyword match ahead.
        assert b._score(crawl, privacy) > b._score(crawl, article)


def test_link_score_ranks_by_region_text_and_url_shape():
    # the scorer itself: a "read more" article link in <main> beats a footer legal
    # link beats a social widget beats a bare icon link.
    from webclient.core.crawl.backing import CrawlBacking

    b = CrawlBacking()
    read_more = b._link_score(
        "Read more", "https://s.example/news/2026/09/the-big-story", "main"
    )
    nav = b._link_score("Products", "https://s.example/products", "nav")
    legal = b._link_score("Privacy Policy", "https://s.example/privacy", "footer")
    social = b._link_score("", "https://twitter.com/acme", "footer")
    icon = b._link_score("", "https://s.example/x", "footer")
    assert read_more > nav > 0
    assert legal < 0 and social < legal  # social widget is the worst
    assert icon < 0


def _path_of(url: str) -> str:
    from urllib.parse import urlparse

    return urlparse(url).path
