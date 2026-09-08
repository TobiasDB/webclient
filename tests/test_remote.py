"""RemoteWebClient tests: drive the service over a real (in-process) uvicorn
server on an ephemeral port -- no browser, exercising the true HTTP path."""
import threading
import time

import pytest
import uvicorn

from webclient import RemoteError, RemoteWebClient, q
from webclient.service import create_app


class _Server:
    def __init__(self, app):
        config = uvicorn.Config(app, host="127.0.0.1", port=0,
                                log_level="warning")
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self) -> str:
        self.thread.start()
        while not self.server.started:
            time.sleep(0.01)
        port = self.server.servers[0].sockets[0].getsockname()[1]
        return f"http://127.0.0.1:{port}"

    def __exit__(self, *exc):
        self.server.should_exit = True
        self.thread.join(timeout=5)

CARDS = """
<html><head><title>Shop</title></head><body>
  <h1>Featured</h1><p>Curated picks.</p>
  <div class="card"><span class="title">Aeropress</span>
    <a class="link" href="/i/1">go</a></div>
  <div class="card"><span class="title">Grinder</span>
    <a class="link" href="/i/2">go</a></div>
</body></html>
"""


@pytest.fixture
def remote(httpserver):
    httpserver.expect_request("/cards").respond_with_data(
        CARDS, content_type="text/html")
    httpserver.expect_request("/i/1").respond_with_json({"name": "Aeropress"})
    httpserver.expect_request("/i/2").respond_with_json({"name": "Grinder"})
    app = create_app(token="secret")
    with _Server(app) as base:
        rc = RemoteWebClient(base, token="secret")
        yield rc, httpserver
        rc.close()
    app.state.wc.close()


def test_fetch_returns_handle(remote):
    rc, server = remote
    doc = rc.ref(server.url_for("/cards")).fetch()
    assert doc.ok and doc.kind == "html" and doc.title == "Shop"
    assert doc.id


def test_auth_enforced(httpserver):
    httpserver.expect_request("/x").respond_with_data("ok")
    app = create_app(token="secret")
    with _Server(app) as base:
        rc = RemoteWebClient(base, token="wrong")
        with pytest.raises(RemoteError, match="401"):
            rc.ref(httpserver.url_for("/x")).fetch()
        rc.close()
    app.state.wc.close()


def test_render_over_the_wire(remote):
    rc, server = remote
    doc = rc.ref(server.url_for("/cards")).fetch()
    assert "# Featured" in doc.markdown
    assert "Curated picks." in doc.text
    assert any(u.endswith("/i/1") for u in doc.links())
    assert isinstance(doc.elements, list)


def test_select_one_level(remote):
    rc, server = remote
    doc = rc.ref(server.url_for("/cards")).fetch()
    assert doc.select(".title") == "Aeropress"
    assert doc.select_all(".title") == ["Aeropress", "Grinder"]
    hrefs = doc.select_all("a", attr="href")
    assert all(u.startswith("http") for u in hrefs)


def test_plan_execution_is_portable(remote):
    rc, server = remote
    plan = (
        q.ref.fetch().select_all(".card")
        .map(title=q.node.select(".title").text,
             link=q.node.select("a").attr("href"))
        .then(name=q.col("link").fetch().json.query("name"))
    )
    rows = plan.collect(rc.ref(server.url_for("/cards")))
    assert sorted(r["name"] for r in rows) == ["Aeropress", "Grinder"]


def test_plan_matches_local_client(remote, httpserver):
    from webclient import WebClient
    rc, server = remote
    plan = q.ref.fetch().select_all(".card").map(
        title=q.node.select(".title").text)
    remote_rows = sorted(r["title"] for r in plan.collect(
        rc.ref(server.url_for("/cards"))))
    with WebClient() as local:
        local_rows = sorted(r["title"] for r in plan.collect(
            local.ref(server.url_for("/cards"))))
    assert remote_rows == local_rows == ["Aeropress", "Grinder"]


def test_sessions(remote, httpserver):
    from werkzeug.wrappers import Response
    rc, server = remote
    server.expect_request("/login").respond_with_response(
        Response("ok", headers={"Set-Cookie": "t=1; Path=/"}))

    def whoami(request):
        return Response(f"t={request.cookies.get('t')}", content_type="text/html")

    server.expect_request("/whoami").respond_with_handler(whoami)
    session = rc.session(ttl=60)
    assert session.status == "running"
    session.ref(server.url_for("/login")).fetch()
    doc = session.ref(server.url_for("/whoami")).fetch()
    assert doc.text == "t=1"
    session.close()
    assert session.status == "closed"


def test_remote_needs_no_browser_or_lxml():
    """The point of the remote client: it must import and build plans with
    the native deps (lxml, playwright) entirely unavailable. Checked in a
    fresh subprocess since sys.modules is polluted by other tests."""
    import subprocess
    import sys

    script = (
        "import builtins\n"
        "_real = builtins.__import__\n"
        "def guard(name, *a, **k):\n"
        "    if name.split('.')[0] in ('lxml', 'playwright'):\n"
        "        raise ImportError(name)\n"
        "    return _real(name, *a, **k)\n"
        "builtins.__import__ = guard\n"
        "from webclient import RemoteWebClient, q\n"
        "plan = q.ref.fetch().select_all('.card').map(t=q.node.select('.t').text)\n"
        "assert plan.to_query().root == 'Reference'\n"
        "import sys\n"
        "assert 'lxml' not in sys.modules and 'playwright' not in sys.modules\n"
        "print('ok')\n"
    )
    result = subprocess.run([sys.executable, "-c", script],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"
