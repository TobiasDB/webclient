"""Remote-backend tests: the same WebClient over a RemoteWebClientCore,
driving a real (in-process) uvicorn server on an ephemeral port -- no local
browser/lxml, exercising the true HTTP path. Remote documents are lazy
handles; value ops run through ``rc.execute`` (deferred/batched)."""

import threading
import time

import httpx
import pytest
import uvicorn

from webclient import (
    Document,
    Reference,
    RemoteError,
    RemoteWebClient,
    RemoteWebClientCore,
    WebClient,
    doc,
    ref,
)
from webclient.service import create_app


class _Server:
    def __init__(self, app):
        config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
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
        CARDS, content_type="text/html"
    )
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
    d = rc.fetch(server.url_for("/cards"))
    assert d.ok and d.kind == "html" and d.title == "Shop"  # cheap meta
    assert d.id


def test_auth_enforced(httpserver):
    httpserver.expect_request("/x").respond_with_data("ok")
    app = create_app(token="secret")
    with _Server(app) as base:
        rc = RemoteWebClient(base, token="wrong")
        with pytest.raises(RemoteError, match="401"):
            rc.fetch(httpserver.url_for("/x"))
        rc.close()
    app.state.wc.close()


def test_render_over_the_wire(remote):
    rc, server = remote
    d = rc.fetch(server.url_for("/cards"))  # eager: one round-trip -> a handle
    assert "# Featured" in d.render("markdown")  # each op round-trips eagerly
    assert "Curated picks." in d.render("text")
    # render("links") -> real References (same as local), reached by .url
    assert any(u.url.endswith("/i/1") for u in d.render("links"))
    assert isinstance(d.render("elements"), list)


def test_eager_doc_ops_round_trip(remote):
    rc, server = remote
    d = rc.fetch(server.url_for("/cards"))
    assert isinstance(d, Document)  # a real Document handle, not a special type
    assert d.ok and d.kind == "html"  # inline metadata, no round-trip
    assert d.title == "Shop"  # a prop op -> one round-trip
    assert d.select(".title").text_content == "Aeropress"  # eager: a value, not a plan
    href = d.select("a").attr("href")  # a single narrowed op -> a real Reference
    assert isinstance(href, Reference) and href.url.endswith("/i/1")
    # a multi-element fan-out is not per-element addressable server-side -- batch
    # it through .lazy (one plan, one round-trip); see the test below.


def test_greedy_eager_ops_warn_recommending_lazy(remote, caplog):
    """Per-op round-trips on a remote document handle are chatty; after a few the
    client nudges (once) toward the .lazy batching interface."""
    rc, server = remote
    d = rc.fetch(server.url_for("/cards"))  # entry fetch -- not counted
    with caplog.at_level("WARNING", logger="webclient"):
        for fmt in ("markdown", "text", "html", "markdown"):  # per-op round-trips
            d.render(fmt)
    nags = [r for r in caplog.records if ".lazy" in r.message]
    assert len(nags) == 1  # warned once, and it points at .lazy


def test_lazy_batches_doc_ops_into_one_call(remote):
    rc, server = remote
    d = rc.fetch(server.url_for("/cards"))
    # d.lazy records the whole chain and runs it in ONE round-trip
    assert d.lazy.select(".title").text_content.collect() == "Aeropress"
    assert d.lazy.select_all(".title").text_content.collect() == ["Aeropress", "Grinder"]
    hrefs = d.lazy.select_all("a").attr("href").collect()
    assert all(u.url.startswith("http") for u in hrefs)


def test_plan_execution_is_portable(remote):
    rc, server = remote
    plan = (
        ref.resolve()
        .select_all(".card")
        .extract(
            title=doc.select(".title").text_content, link=doc.select("a").attr("href")
        )
        .extract(name=doc.reference("link").resolve().select("name").attr("value"))
        .project()
    )
    rows = plan.collect(rc.ref(server.url_for("/cards")))
    assert sorted(r["name"] for r in rows) == ["Aeropress", "Grinder"]


def test_plan_matches_local_client(remote, httpserver):
    rc, server = remote
    plan = (
        ref.resolve()
        .select_all(".card")
        .extract(title=doc.select(".title").text_content)
        .project()
    )
    remote_rows = sorted(
        r["title"] for r in plan.collect(rc.ref(server.url_for("/cards")))
    )
    with WebClient() as local:
        local_rows = sorted(
            r["title"] for r in plan.collect(local.ref(server.url_for("/cards")))
        )
    assert remote_rows == local_rows == ["Aeropress", "Grinder"]


def test_same_facade_over_a_remote_core(remote):
    """The remote client is literally a WebClient over a remote core."""
    rc, server = remote
    assert isinstance(rc, WebClient)
    assert isinstance(rc.core, RemoteWebClientCore)


def test_sessions(remote, httpserver):
    from werkzeug.wrappers import Response

    rc, server = remote
    server.expect_request("/login").respond_with_response(
        Response("ok", headers={"Set-Cookie": "t=1; Path=/"})
    )

    def whoami(request):
        return Response(f"t={request.cookies.get('t')}", content_type="text/html")

    server.expect_request("/whoami").respond_with_handler(whoami)
    session = rc.session(ttl=60)
    assert session.status == "running"
    session.fetch(server.url_for("/login"))  # eager -> sets a cookie server-side
    d = session.fetch(server.url_for("/whoami"))  # resolved through the session
    assert d.render("text").strip() == "t=1"
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
        "from webclient import RemoteWebClient, doc, ref\n"
        "plan = ref.resolve().select_all('.card').extract(t=doc.select('.t').text_content)\n"
        "assert plan._plan.root == 'Reference'\n"
        "import sys\n"
        "assert 'lxml' not in sys.modules and 'playwright' not in sys.modules\n"
        "print('ok')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"


def test_remote_client_times_out_on_a_hung_service(httpserver):
    """The remote client bounds every round-trip by its timeout, so a hung
    service raises instead of blocking the caller forever."""
    from werkzeug.wrappers import Response

    def slow(request):
        time.sleep(0.5)
        return Response("{}", content_type="application/json")

    httpserver.expect_request("/execute").respond_with_handler(slow)
    rc = RemoteWebClientCore(url=httpserver.url_for(""), timeout=0.1)  # IS the client
    with pytest.raises(httpx.TimeoutException):
        rc.fetch("https://example.com")
    rc.close()


def test_remote_surfaces_a_structured_error(remote):
    """A server-side fetch failure comes back as a RemoteError carrying the
    structured WebError, so `.error.retriable` works the same as locally."""
    rc, server = remote
    server.expect_request("/boom").respond_with_data("no", status=500)
    with pytest.raises(RemoteError) as info:
        rc.fetch(server.url_for("/boom"))
    err = info.value.error
    assert err is not None
    assert err.type == "HTTPStatus"
    assert err.status_code == 500 and err.retriable is True
