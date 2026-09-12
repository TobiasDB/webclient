import pytest
from fastapi.testclient import TestClient

from webclient import WebClient, doc, ref
from webclient.service import create_app

CARDS = """
<html><head><title>Shop</title></head><body>
  <h1>Aeropress</h1><p>Featured brewer.</p>
  <div class="card"><span class="title">Aeropress</span>
    <a href="/i/1">go</a></div>
  <div class="card"><span class="title">Grinder</span>
    <a href="/i/2">go</a></div>
</body></html>
"""


@pytest.fixture
def client_and_server(httpserver):
    httpserver.expect_request("/cards").respond_with_data(
        CARDS, content_type="text/html")
    httpserver.expect_request("/i/1").respond_with_json({"name": "Aeropress"})
    httpserver.expect_request("/i/2").respond_with_json({"name": "Grinder"})
    wc = WebClient()
    app = create_app(wc, token="secret")
    with TestClient(app) as api:
        yield api, httpserver
    wc.close()


AUTH = {"Authorization": "Bearer secret"}


def _handle(api, url):
    """Fetch = execute ``ref.resolve()``; the Document comes back as a handle."""
    rows = api.post("/execute", headers=AUTH, json={
        "plan": ref.resolve()._plan.model_dump(), "url": url}).json()["rows"]
    return rows["__doc__"]


def test_auth_required(client_and_server):
    api, server = client_and_server
    assert api.post("/execute", json={
        "plan": ref.resolve()._plan.model_dump(),
        "url": server.url_for("/cards")}).status_code == 401


def test_fetch_returns_handle_not_html(client_and_server):
    api, server = client_and_server
    meta = _handle(api, server.url_for("/cards"))
    assert meta["ok"] and meta["kind"] == "html" and meta["title"] == "Shop"
    assert "content" not in meta and "html" not in meta   # handle only
    assert meta["id"]


def _exec(api, doc_id, expr):
    return api.post("/execute", headers=AUTH, json={
        "plan": expr._plan.model_dump(), "document_id": doc_id}).json()["rows"]


def test_render_via_execute(client_and_server):
    api, server = client_and_server
    doc_id = _handle(api, server.url_for("/cards"))["id"]
    assert "Aeropress" in _exec(api, doc_id, doc.render("markdown"))
    links = _exec(api, doc_id, doc.render("links"))
    assert any(u.endswith("/i/1") for u in links)
    assert isinstance(_exec(api, doc_id, doc.render("elements")), list)


def test_select_via_execute(client_and_server):
    api, server = client_and_server
    doc_id = _handle(api, server.url_for("/cards"))["id"]
    assert _exec(api, doc_id, doc.select(".title").attr("text")) == "Aeropress"
    assert _exec(api, doc_id, doc.select_all(".title").attr("text")) == \
        ["Aeropress", "Grinder"]
    hrefs = _exec(api, doc_id, doc.select_all("a").attr("href"))
    assert all(u.startswith("http") for u in hrefs)


def test_session_lifecycle(client_and_server):
    api, _ = client_and_server
    created = api.post("/sessions", json={"ttl": 60}, headers=AUTH).json()
    assert created["status"] == "running"
    sid = created["id"]
    assert api.get(f"/sessions/{sid}", headers=AUTH).json()["status"] == "running"
    assert api.delete(f"/sessions/{sid}", headers=AUTH).json()["status"] == "closed"


def test_plan_submission(client_and_server):
    api, server = client_and_server
    plan = (
        ref.resolve().select_all(".card")
        .extract(title=doc.select(".title").attr("text"),
                 link=doc.select("a").attr("href"))
        .extract(name=doc.reference("link").resolve().select("name").attr("value"))
        .project()
    )._plan
    resp = api.post("/execute", headers=AUTH, json={
        "plan": plan.model_dump(), "url": server.url_for("/cards")})
    rows = resp.json()["rows"]
    names = sorted(r["name"] for r in rows)
    assert names == ["Aeropress", "Grinder"]


def test_plan_with_unknown_op_is_rejected_not_dispatched(client_and_server):
    api, server = client_and_server
    plan = {"root": "Document", "steps": [{"kind": "get", "name": "__class__"}]}
    resp = api.post("/execute", headers=AUTH, json={
        "plan": plan, "url": server.url_for("/cards")})
    assert resp.status_code == 422 and "__class__" in resp.text


def test_missing_document_404(client_and_server):
    api, _ = client_and_server
    assert api.get("/document/nope", headers=AUTH).status_code == 404


def test_events_websocket_streams_and_resumes(client_and_server):
    api, server = client_and_server
    with api.websocket_connect("/events?topic=network") as ws:
        api.post("/execute", headers=AUTH, json={
            "plan": ref.resolve()._plan.model_dump(),
            "url": server.url_for("/cards")})
        msg = ws.receive_json()
        assert msg["topic"].startswith("network")
        assert msg["seq"] is not None
        assert msg["source"] == "core-network"


def test_search_as_a_plan(httpserver):
    """Search is not a special endpoint: it is a projected plan submitted to
    /execute, exactly as the client's ``search`` builds it."""
    httpserver.expect_request("/s").respond_with_data(
        '<div class="result"><a class="result__a" href="/g/1">First</a></div>',
        content_type="text/html")
    plan = (ref.resolve().select_all(".result", limit=1)
            .extract(title=doc.select(".result__a").attr("text"),
                     url=doc.select(".result__a").attr("href")).project())
    wc = WebClient()
    app = create_app(wc, token="secret")
    with TestClient(app) as api:
        rows = api.post("/execute", headers=AUTH, json={
            "plan": plan._plan.model_dump(),
            "url": httpserver.url_for("/s") + "?q=x"}).json()["rows"]
        assert [r["title"] for r in rows] == ["First"]
    wc.close()


def test_crawl_is_not_implemented(client_and_server):
    api, _ = client_and_server
    assert api.post("/crawl", headers=AUTH).status_code == 501
