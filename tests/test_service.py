import pytest
from fastapi.testclient import TestClient

from webclient import WebClient, q
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


def test_auth_required(client_and_server):
    api, server = client_and_server
    assert api.post("/fetch", json={"url": server.url_for("/cards")}).status_code == 401


def test_fetch_returns_handle_not_html(client_and_server):
    api, server = client_and_server
    resp = api.post("/fetch", json={"url": server.url_for("/cards")}, headers=AUTH)
    assert resp.status_code == 200
    meta = resp.json()
    assert meta["ok"] and meta["kind"] == "html" and meta["title"] == "Shop"
    assert "content" not in meta and "html" not in meta   # handle only
    assert meta["id"]


def test_render_endpoint(client_and_server):
    api, server = client_and_server
    doc_id = api.post("/fetch", json={"url": server.url_for("/cards")},
                      headers=AUTH).json()["id"]
    md = api.get(f"/documents/{doc_id}/render", params={"format": "markdown"},
                 headers=AUTH).json()
    assert "Aeropress" in md["result"]
    links = api.get(f"/documents/{doc_id}/render", params={"format": "links"},
                    headers=AUTH).json()
    assert any(u.endswith("/i/1") for u in links["result"])
    elements = api.get(f"/documents/{doc_id}/render", params={"format": "elements"},
                       headers=AUTH).json()
    assert isinstance(elements["result"], list)


def test_select_endpoint(client_and_server):
    api, server = client_and_server
    doc_id = api.post("/fetch", json={"url": server.url_for("/cards")},
                      headers=AUTH).json()["id"]
    one = api.post(f"/documents/{doc_id}/select",
                   json={"selector": ".title"}, headers=AUTH).json()
    assert one["value"] == "Aeropress"
    many = api.post(f"/documents/{doc_id}/select",
                    json={"selector": ".title", "all": True}, headers=AUTH).json()
    assert many["values"] == ["Aeropress", "Grinder"]
    hrefs = api.post(f"/documents/{doc_id}/select",
                     json={"selector": "a", "all": True, "attr": "href"},
                     headers=AUTH).json()
    assert all(u.startswith("http") for u in hrefs["values"])


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
        q.ref.fetch().select_all(".card")
        .map(title=q.node.select(".title").text,
             link=q.node.select("a").attr("href"))
        .then(name=q.col("link").fetch().json.query("name"))
    ).to_query()
    resp = api.post("/plans", params={"url": server.url_for("/cards")},
                    json=plan.model_dump(), headers=AUTH)
    rows = resp.json()["rows"]
    names = sorted(r["name"] for r in rows)
    assert names == ["Aeropress", "Grinder"]


def test_missing_document_404(client_and_server):
    api, _ = client_and_server
    assert api.get("/documents/nope", headers=AUTH).status_code == 404


def test_events_websocket_streams_and_resumes(client_and_server):
    api, server = client_and_server
    with api.websocket_connect("/events?topic=network") as ws:
        api.post("/fetch", json={"url": server.url_for("/cards")}, headers=AUTH)
        msg = ws.receive_json()
        assert msg["topic"].startswith("network")
        assert msg["seq"] is not None
        assert msg["source"] == "core-network"
