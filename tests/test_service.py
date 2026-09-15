import time

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
        CARDS, content_type="text/html"
    )
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
    rows = api.post(
        "/execute",
        headers=AUTH,
        json={"plan": ref.resolve()._plan.model_dump(), "url": url},
    ).json()["rows"]
    return rows["__doc__"]


def test_doc_store_is_lru_bounded():
    """The server's document store evicts least-recently-used entries so it does
    not grow without bound."""
    from webclient.service import _DocStore

    s: _DocStore = _DocStore(2)
    s["a"], s["b"] = object(), object()
    s["c"] = object()  # over cap -> evict LRU "a"
    assert list(s) == ["b", "c"]
    _ = s["b"]  # touch -> "c" is now the LRU
    s["d"] = object()
    assert list(s) == ["b", "d"]


def test_session_store_is_capped():
    """New sessions beyond the cap are rejected (429) rather than leaked."""
    wc = WebClient()
    app = create_app(wc, token="secret", max_sessions=1)
    with TestClient(app) as api:
        assert api.post("/sessions", headers=AUTH, json={}).status_code == 200
        assert api.post("/sessions", headers=AUTH, json={}).status_code == 429
    wc.close()


def test_expired_sessions_are_reclaimed():
    """A past-ttl session is swept on the next create, freeing cap room."""
    wc = WebClient()
    app = create_app(wc, token="secret", max_sessions=1)
    with TestClient(app) as api:
        assert (
            api.post("/sessions", headers=AUTH, json={"ttl": 0.01}).status_code == 200
        )
        time.sleep(0.05)
        assert (
            api.post("/sessions", headers=AUTH, json={"ttl": 0.01}).status_code == 200
        )
    wc.close()


def test_auth_required(client_and_server):
    api, server = client_and_server
    assert (
        api.post(
            "/execute",
            json={
                "plan": ref.resolve()._plan.model_dump(),
                "url": server.url_for("/cards"),
            },
        ).status_code
        == 401
    )


def test_fetch_returns_handle_not_html(client_and_server):
    api, server = client_and_server
    meta = _handle(api, server.url_for("/cards"))
    assert meta["ok"] and meta["kind"] == "html"  # lightweight handle
    assert "content" not in meta and "html" not in meta  # handle only
    assert meta["id"]


def _exec(api, doc_id, expr):
    return api.post(
        "/execute",
        headers=AUTH,
        json={"plan": expr._plan.model_dump(), "document_id": doc_id},
    ).json()["rows"]


def test_render_via_execute(client_and_server):
    api, server = client_and_server
    doc_id = _handle(api, server.url_for("/cards"))["id"]
    assert "Aeropress" in _exec(api, doc_id, doc.render("markdown"))
    links = _exec(api, doc_id, doc.render("links"))  # References -> {"__ref__": spec}
    assert any(u["__ref__"]["path"] == "/i/1" for u in links)
    assert isinstance(_exec(api, doc_id, doc.render("elements")), list)


def test_select_via_execute(client_and_server):
    api, server = client_and_server
    doc_id = _handle(api, server.url_for("/cards"))["id"]
    assert _exec(api, doc_id, doc.select(".title").text_content) == "Aeropress"
    assert _exec(api, doc_id, doc.select_all(".title").text_content) == [
        "Aeropress",
        "Grinder",
    ]
    hrefs = _exec(api, doc_id, doc.select_all("a").attr("href"))  # -> {"__ref__": spec}
    assert all(u["__ref__"]["scheme"] in ("http", "https") for u in hrefs)


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
        ref.resolve()
        .select_all(".card")
        .extract(
            title=doc.select(".title").text_content, link=doc.select("a").attr("href")
        )
        .extract(name=doc.reference("link").resolve().select("name").attr("value"))
        .project()
    )._plan
    resp = api.post(
        "/execute",
        headers=AUTH,
        json={"plan": plan.model_dump(), "url": server.url_for("/cards")},
    )
    rows = resp.json()["rows"]
    names = sorted(r["name"] for r in rows)
    assert names == ["Aeropress", "Grinder"]


def test_plan_with_unknown_op_is_rejected_not_dispatched(client_and_server):
    api, server = client_and_server
    plan = {"root": "Document", "steps": [{"kind": "get", "name": "__class__"}]}
    resp = api.post(
        "/execute", headers=AUTH, json={"plan": plan, "url": server.url_for("/cards")}
    )
    assert resp.status_code == 422 and "__class__" in resp.text


def test_execute_returns_structured_error_on_invalid_plan(client_and_server):
    api, server = client_and_server
    plan = {"root": "Document", "steps": [{"kind": "get", "name": "__class__"}]}
    resp = api.post(
        "/execute", headers=AUTH, json={"plan": plan, "url": server.url_for("/cards")}
    )
    assert resp.status_code == 422
    err = resp.json()["error"]
    assert err["type"] == "InvalidPlan" and err["retriable"] is False
    assert (
        "__class__" in err["message"] and err["hint"]
    )  # actionable, not a bare string


def test_execute_returns_structured_error_on_missing_document(client_and_server):
    api, _ = client_and_server
    plan = {"root": "Document", "steps": [{"kind": "get", "name": "title"}]}
    resp = api.post(
        "/execute", headers=AUTH, json={"plan": plan, "document_id": "gone"}
    )
    assert resp.status_code == 404
    err = resp.json()["error"]
    assert err["type"] == "NoSuchDocument" and err["hint"]


def test_missing_document_404(client_and_server):
    api, _ = client_and_server
    resp = api.get("/document/nope", headers=AUTH)
    assert resp.status_code == 404
    err = resp.json()["error"]
    assert err["type"] == "NoSuchDocument" and err["hint"]


def test_session_endpoints_return_structured_404(client_and_server):
    api, _ = client_and_server
    for resp in (
        api.get("/sessions/gone", headers=AUTH),
        api.delete("/sessions/gone", headers=AUTH),
    ):
        assert resp.status_code == 404
        err = resp.json()["error"]
        assert err["type"] == "NoSuchSession" and err["hint"]


def test_session_cap_returns_structured_error():
    wc = WebClient()
    app = create_app(wc, token="secret", max_sessions=1)
    with TestClient(app) as api:
        assert api.post("/sessions", headers=AUTH, json={}).status_code == 200
        resp = api.post("/sessions", headers=AUTH, json={})
        assert resp.status_code == 429
        err = resp.json()["error"]
        assert err["type"] == "TooManySessions" and err["retriable"] is True
    wc.close()


def test_crawl_requires_a_url(client_and_server):
    api, _ = client_and_server
    resp = api.post("/crawl", headers=AUTH, json={})
    assert resp.status_code == 422
    err = resp.json()["error"]
    assert err["type"] == "InvalidRequest" and err["hint"]


def test_events_websocket_streams_and_resumes(client_and_server):
    api, server = client_and_server
    with api.websocket_connect("/events?topic=network") as ws:
        api.post(
            "/execute",
            headers=AUTH,
            json={
                "plan": ref.resolve()._plan.model_dump(),
                "url": server.url_for("/cards"),
            },
        )
        msg = ws.receive_json()
        assert msg["topic"].startswith("network")
        assert msg["seq"] is not None
        assert msg["source"] == "core-network"


def test_search_as_a_plan(httpserver):
    """Search is not a special endpoint: it is a projected plan submitted to
    /execute, exactly as the client's ``search`` builds it."""
    httpserver.expect_request("/s").respond_with_data(
        '<div class="result"><a class="result__a" href="/g/1">First</a></div>',
        content_type="text/html",
    )
    plan = (
        ref.resolve()
        .select_all(".result", limit=1)
        .extract(
            title=doc.select(".result__a").text_content,
            url=doc.select(".result__a").attr("href"),
        )
        .project()
    )
    wc = WebClient()
    app = create_app(wc, token="secret")
    with TestClient(app) as api:
        rows = api.post(
            "/execute",
            headers=AUTH,
            json={
                "plan": plan._plan.model_dump(),
                "url": httpserver.url_for("/s") + "?q=x",
            },
        ).json()["rows"]
        assert [r["title"] for r in rows] == ["First"]
    wc.close()


def test_crawl_returns_page_summaries(client_and_server):
    api, server = client_and_server
    resp = api.post(
        "/crawl", headers=AUTH, json={"url": server.url_for("/cards"), "max_pages": 5}
    )
    assert resp.status_code == 200
    data = resp.json()
    # crawled the seed and its same-origin links (/i/1, /i/2)
    assert len(data["pages"]) >= 2
    assert any(u.endswith("/cards") for u in data["urls"])
    assert data["done"] is True


def test_sitemap_maps_a_domain(client_and_server):
    api, server = client_and_server
    resp = api.post(
        "/sitemap", headers=AUTH, json={"url": server.url_for("/cards"), "depth": 2}
    )
    assert resp.status_code == 200
    urls = resp.json()["urls"]
    assert any(u.endswith("/cards") for u in urls)


def test_crawl_runs_on_a_named_session(client_and_server):
    api, server = client_and_server
    sid = api.post("/sessions", headers=AUTH, json={}).json()["id"]
    resp = api.post(
        "/crawl",
        headers=AUTH,
        json={"url": server.url_for("/cards"), "session": sid, "max_pages": 3},
    )
    assert resp.status_code == 200 and len(resp.json()["pages"]) >= 1


def test_crawl_with_unknown_session_is_404(client_and_server):
    api, server = client_and_server
    resp = api.post(
        "/crawl",
        headers=AUTH,
        json={"url": server.url_for("/cards"), "session": "sess-nope"},
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["type"] == "NoSuchSession"


def test_execute_returns_structured_error_on_upstream_failure(client_and_server):
    api, server = client_and_server
    server.expect_request("/boom").respond_with_data("no", status=500)
    r = api.post(
        "/execute",
        headers=AUTH,
        json={"plan": ref.resolve()._plan.model_dump(), "url": server.url_for("/boom")},
    )
    assert r.status_code == 502
    err = r.json()["error"]
    assert err["type"] == "HTTPStatus"
    assert err["status_code"] == 500 and err["retriable"] is True


def test_execute_ssrf_guard_blocks_loopback():
    app = create_app(token="secret", block_private_hosts=True)
    with TestClient(app) as api:
        r = api.post(
            "/execute",
            headers=AUTH,
            json={
                "plan": ref.resolve()._plan.model_dump(),
                "url": "http://127.0.0.1:9/x",
            },
        )
        assert r.status_code == 502 and r.json()["error"]["type"] == "BlockedHost"
    app.state.wc.close()


# -- task verbs + plan authoring endpoint --------------------------------------


def test_task_verb_markdown_and_links(client_and_server):
    api, server = client_and_server
    url = server.url_for("/cards")
    md = api.post("/markdown", headers=AUTH, json={"url": url}).json()["result"]
    assert "# Aeropress" in md
    links = api.post("/links", headers=AUTH, json={"url": url}).json()["result"]
    assert any(u.endswith("/i/1") for u in links)


def test_task_verb_summary_selects_facets(client_and_server):
    api, server = client_and_server
    r = api.post(
        "/summary",
        headers=AUTH,
        json={"url": server.url_for("/cards"), "facets": ["transport", "metadata"]},
    ).json()["result"]
    assert r["transport"] and r["metadata"] and r["structure"] is None


def test_task_verb_missing_url_is_422(client_and_server):
    api, _ = client_and_server
    r = api.post("/markdown", headers=AUTH, json={})
    assert r.status_code == 422 and r.json()["error"]["type"] == "InvalidRequest"


def test_plan_endpoint_validates_describes_and_runs(client_and_server):
    api, server = client_and_server
    plan = ref.resolve().select("h1").text_content._plan.model_dump()
    # validate + describe + blob, no run
    v = api.post("/plan", headers=AUTH, json={"plan": plan}).json()
    assert v["valid"] and v["describe"] == "Reference.resolve().select('h1').text_content"
    import json as _json

    assert isinstance(_json.loads(v["blob"]), dict)  # blob is plain JSON
    # the returned blob round-trips through the same endpoint and can run
    r = api.post(
        "/plan",
        headers=AUTH,
        json={"blob": v["blob"], "url": server.url_for("/cards"), "run": True},
    ).json()
    assert r["rows"] == "Aeropress"


def test_plan_endpoint_rejects_a_bad_plan(client_and_server):
    api, _ = client_and_server
    r = api.post(
        "/plan",
        headers=AUTH,
        json={"plan": {"root": "Document", "steps": [{"kind": "get", "name": "_x"}]}},
    )
    assert r.status_code == 422 and r.json()["error"]["type"] == "InvalidPlan"


def test_skeleton_endpoint(client_and_server):
    api, server = client_and_server
    r = api.post("/skeleton", headers=AUTH, json={"url": server.url_for("/cards")}).json()
    assert '<div class="card">' in r["result"] and '<span class="title">' in r["result"]
