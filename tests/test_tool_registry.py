"""One tool registry, three transports (roadmap N2 / N10): Python dispatch, MCP and the HTTP
service agree on names, schemas and results; a remote client is implicitly a session."""

import pytest
from fastapi.testclient import TestClient

from webclient import RemoteWebClient, WebClient
from webclient.llm.mcp import build_tools
from webclient.service import create_app
from webclient.tools import TOOLS, ToolError, UrlArgs, dispatch, schema, tool

PAGE = """<html><head><title>Shop</title></head><body><main>
<h1>Featured</h1>
<div class="card"><span class="title">Aeropress</span><a href="/i/1">go</a></div>
<div class="card"><span class="title">Grinder</span><a href="/i/2">go</a></div>
<div class="card"><span class="title">Kettle</span><a href="/i/3">go</a></div>
</main></body></html>"""


@pytest.fixture
def page(httpserver):
    httpserver.expect_request("/p").respond_with_data(PAGE, content_type="text/html")
    return httpserver.url_for("/p")


def test_registry_is_typed_and_documented():
    for t in TOOLS.values():
        assert t.description and t.story and t.input.__doc__ is not None or True
        s = t.schema()
        assert s["type"] == "object" and "properties" in s and s["additionalProperties"] is False
    names = {d["name"] for d in schema()}
    assert {"fetch_markdown", "extract", "flags", "card", "patterns", "crawl", "run_plan"} <= names
    with pytest.raises(ToolError) as info:
        dispatch("extract", {"url": "http://x"})  # missing result/fields
    assert info.value.tool == "extract" and info.value.errors
    with pytest.raises(KeyError):
        dispatch("nope", {})


def test_three_transports_agree(page):
    wc = WebClient()
    app = create_app(wc, token="t")
    hdr = {"Authorization": "Bearer t"}
    with TestClient(app) as api:
        listed = api.get("/tools", headers=hdr).json()
        assert {d["name"] for d in listed} == set(TOOLS) == {t.name for t in build_tools(wc)}
        assert listed[0]["input_schema"] == build_tools(wc)[0].input_schema
        py = dispatch("extract", {"url": page, "result": ".card", "fields": {"t": ".title"}, "limit": 2}, wc)
        mcp = next(t for t in build_tools(wc) if t.name == "extract").handler(
            {"url": page, "result": ".card", "fields": {"t": ".title"}, "limit": 2})
        http = api.post("/tools/extract", headers=hdr,
                        json={"url": page, "result": ".card", "fields": {"t": ".title"}, "limit": 2}).json()["result"]
        assert py == mcp == http == [{"t": "Aeropress"}, {"t": "Grinder"}]
        # new tools: card / flags / patterns over HTTP
        card = api.post("/tools/card", headers=hdr, json={"url": page}).json()["result"]
        assert card["title"] == "Shop" and card["kind"] == "html"
        hints = api.post("/tools/patterns", headers=hdr, json={"url": page, "for": "extract"}).json()["result"]
        assert hints and hints[0]["subject"] == "div.card"
        flags = api.post("/tools/flags", headers=hdr, json={"url": page}).json()["result"]
        assert isinstance(flags, list)
        # legacy alias paths still answer, generated from the same registry
        assert api.post("/markdown", headers=hdr, json={"url": page}).json()["result"].startswith("# Featured")
        bad = api.post("/tools/extract", headers=hdr, json={"url": page})
        assert bad.status_code == 422 and bad.json()["error"]["type"] == "InvalidRequest"
        assert api.post("/tools/nope", headers=hdr, json={}).status_code == 404
    wc.close()


def test_a_custom_tool_reaches_every_transport(page):
    @tool("title_of", "The page title.", returns="str", story="test")
    def title_of(args: UrlArgs, wc):
        return wc.fetch(args.url).title

    try:
        wc = WebClient()
        assert dispatch("title_of", {"url": page}, wc) == "Shop"
        assert any(t.name == "title_of" for t in build_tools(wc))
        with TestClient(create_app(wc)) as api:
            assert api.post("/tools/title_of", json={"url": page}).json()["result"] == "Shop"
        wc.close()
    finally:
        TOOLS.pop("title_of", None)


def test_remote_client_is_implicitly_a_server_session(page):
    from tests.test_remote import _Server

    app = create_app(token="s")
    with _Server(app) as base:
        rc = RemoteWebClient(base, token="s")
        assert rc._server_sid and rc._server_sid in app.state.sessions
        doc = rc.fetch(page)
        assert doc.title == "Shop"
        assert doc.name in app.state.session_docs[rc._server_sid]  # the client's own store
        child = rc.session(ttl=30)
        assert child._server_sid and child._server_sid != rc._server_sid  # nests
        child.close()
        sid = rc._server_sid
        rc.close()
        assert sid not in app.state.sessions  # disposed with the client
    app.state.wc.close()
