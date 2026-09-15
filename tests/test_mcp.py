"""The MCP adapter (webclient.mcp): the task verbs + plan machinery as MCP tools.

The tool registry is plain data + handlers, so it is exercised here with no MCP
SDK installed -- via ``dispatch`` (the same call the stdio server would make).
"""

import pytest

from webclient import WebClient, ref
from webclient.mcp import build_tools, dispatch

PAGE = """
<html><head><title>Shop</title></head><body>
  <h1>Aeropress</h1><p>Curated picks.</p>
  <div class="card"><span class="title">Aeropress</span><a href="/i/1">go</a></div>
  <div class="card"><span class="title">Grinder</span><a href="/i/2">go</a></div>
</body></html>
"""


@pytest.fixture
def wc():
    with WebClient() as client:
        yield client


def test_tool_registry_is_well_formed(wc):
    tools = build_tools(wc)
    names = {t.name for t in tools}
    assert {"fetch_markdown", "links", "summary", "crawl", "sitemaps",
            "validate_plan", "run_plan"} <= names
    for t in tools:  # every tool has a JSON-schema object with properties
        assert t.input_schema["type"] == "object" and "properties" in t.input_schema
        assert "_required" not in str(t.input_schema)  # the marker is stripped


def test_fetch_markdown_and_links_tools(httpserver, wc):
    httpserver.expect_request("/p").respond_with_data(PAGE, content_type="text/html")
    url = httpserver.url_for("/p")
    md = dispatch("fetch_markdown", {"url": url}, wc)
    assert "# Aeropress" in md
    urls = dispatch("links", {"url": url}, wc)
    assert any(u.endswith("/i/1") for u in urls)


def test_summary_tool_selects_facets(httpserver, wc):
    httpserver.expect_request("/p").respond_with_data(PAGE, content_type="text/html")
    s = dispatch("summary", {"url": httpserver.url_for("/p"), "facets": ["metadata"]}, wc)
    assert s["metadata"]["title"] == "Shop" and s["transport"] is None


def test_validate_and_run_plan_tools(httpserver, wc):
    httpserver.expect_request("/p").respond_with_data(PAGE, content_type="text/html")
    blob = ref.resolve().select("h1").text_content.to_blob()
    v = dispatch("validate_plan", {"blob": blob}, wc)
    assert v["valid"] and v["describe"] == "Reference.resolve().select('h1').text_content"
    out = dispatch("run_plan", {"blob": blob, "url": httpserver.url_for("/p")}, wc)
    assert out == "Aeropress"


def test_unknown_tool_raises(wc):
    with pytest.raises(KeyError):
        dispatch("nope", {}, wc)


def test_build_server_without_sdk_gives_a_clear_error(wc):
    # the mcp SDK is not installed in the gate env -- the error names the fix.
    pytest.importorskip  # noqa: B018  (keep import available for other tests)
    from webclient.mcp import build_server

    try:
        import mcp  # noqa: F401
    except ImportError:
        with pytest.raises(ImportError, match="pip install mcp"):
            build_server(wc)
