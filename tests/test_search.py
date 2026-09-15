"""Robust search: provider fallback, result-shape parsing, ad/junk filtering.

Live engines are exercised elsewhere; here providers point at a local server so
the fallback + parsing are deterministic.
"""

import pytest

from webclient import RETURN, WebClient, WebException
from webclient.core.client import search as S

# a DuckDuckGo-html-shaped results page: two organic hits (redirect-wrapped) and
# one ad row whose href stays on duckduckgo.com.
DDG_HTML = """
<html><body>
<div class="result"><a class="result__a"
  href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fa">Title A</a>
  <a class="result__snippet">Snippet A</a></div>
<div class="result result--ad"><a class="result__a"
  href="https://duckduckgo.com/y.js?ad_domain=x">Sponsored</a></div>
<div class="result"><a class="result__a"
  href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fb">Title B</a></div>
</body></html>
"""

# a DuckDuckGo-lite-shaped page: flat anchors + a parallel snippet list.
DDG_LITE = """
<html><body><table>
<tr><td><a class="result-link"
  href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fx">X site</a></td></tr>
<tr><td class="result-snippet">Snippet X</td></tr>
<tr><td><a class="result-link"
  href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fy">Y site</a></td></tr>
<tr><td class="result-snippet">Snippet Y</td></tr>
</table></body></html>
"""


@pytest.fixture
def wc():
    with WebClient() as client:
        yield client


def _provider(name, path, server, mode="container"):
    row, link, snip = (
        ("a.result-link", "", ".result-snippet") if mode == "flat"
        else (".result", ".result__a", ".result__snippet")
    )
    return S._Provider(name, server.url_for(path) + "?q={q}", row, link, snip, mode)


def test_container_parsing_and_ad_filtering(httpserver, wc):
    httpserver.expect_request("/p").respond_with_data(DDG_HTML, content_type="text/html")
    doc = wc.fetch(httpserver.url_for("/p"))
    hits = S._parse(doc, _provider("p", "/p", httpserver), 10)
    assert [h.title for h in hits] == ["Title A", "Title B"]        # the ad row dropped
    assert hits[0].url == "https://example.com/a"                   # redirect unwrapped
    assert hits[0].description == "Snippet A"


def test_flat_parsing_pairs_snippets_by_index(httpserver, wc):
    httpserver.expect_request("/l").respond_with_data(DDG_LITE, content_type="text/html")
    doc = wc.fetch(httpserver.url_for("/l"))
    hits = S._parse(doc, _provider("l", "/l", httpserver, "flat"), 10)
    assert [(h.title, h.url, h.description) for h in hits] == [
        ("X site", "https://example.com/x", "Snippet X"),
        ("Y site", "https://example.com/y", "Snippet Y"),
    ]


def test_search_falls_back_when_a_provider_is_blocked(httpserver, wc, monkeypatch):
    # first provider 503s, second serves results -> search transparently falls over.
    httpserver.expect_request("/blocked").respond_with_data("no", status=503)
    httpserver.expect_request("/ok").respond_with_data(DDG_HTML, content_type="text/html")
    monkeypatch.setattr(S, "_PROVIDERS", (
        _provider("blocked", "/blocked", httpserver),
        _provider("ok", "/ok", httpserver),
    ))
    hits = wc.search("q", limit=5)
    assert [h.title for h in hits] == ["Title A", "Title B"]


def test_search_raises_when_all_providers_fail(httpserver, wc, monkeypatch):
    httpserver.expect_request("/b1").respond_with_data("no", status=503)
    httpserver.expect_request("/b2").respond_with_data("no", status=429)
    monkeypatch.setattr(S, "_PROVIDERS", (
        _provider("b1", "/b1", httpserver), _provider("b2", "/b2", httpserver),
    ))
    with pytest.raises(WebException):
        wc.search("q")
    assert wc.search("q", optional=True) == []      # opt-in lenient -> []
    assert wc.search("q", error=RETURN) == []


def test_search_sends_a_browser_user_agent(httpserver, wc, monkeypatch):
    # engines block a library UA -- every search request must carry a browser UA.
    seen: dict[str, str] = {}

    def handler(request):
        from werkzeug.wrappers import Response

        seen.update({k.lower(): v for k, v in request.headers.items()})
        return Response(DDG_HTML, content_type="text/html")

    httpserver.expect_request("/s").respond_with_handler(handler)
    monkeypatch.setattr(S, "_PROVIDERS", (_provider("s", "/s", httpserver),))
    wc.search("q")
    assert "mozilla/5.0" in seen.get("user-agent", "").lower()
