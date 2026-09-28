"""Errors are catalogued, problem-details shaped, and bound to the op/subject (roadmap N5)."""

import ast
import pathlib

import pytest

import webclient
from webclient import WebClient, WebError
from webclient.kernel.errors import CATALOG, REMEDIES, error_for, make, select_error


def test_every_code_used_in_the_package_is_catalogued():
    used: set[str] = set()
    pkg = pathlib.Path(webclient.__file__).parent
    for py in pkg.rglob("*.py"):
        tree = ast.parse(py.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "make":
                if node.args and isinstance(node.args[0], ast.Constant):
                    used.add(node.args[0].value)
            if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "select_error":
                for kw in node.keywords:
                    if kw.arg == "code" and isinstance(kw.value, ast.Constant):
                        used.add(kw.value.value)
    missing = used - set(CATALOG)
    assert not missing, missing
    assert {s.remedy for s in CATALOG.values()} <= set(REMEDIES)
    assert len({s.code for s in CATALOG.values()}) == len(CATALOG)


def test_make_fills_the_catalogue_defaults_and_overrides_win():
    err = make("fetch.login_required", "wall", op="fetch", subject="doc:1")
    assert err.type == "LoginRequired" and err.code == "fetch.login_required"
    assert err.remedy == "credentials" and err.hint and not err.retriable
    assert err.op == "fetch" and err.subject == "doc:1" and err.message == "wall"
    assert make("service.no_document").message == "Document handle gone"  # title fallback
    assert make("fetch.http_status", status_code=503, retriable=True).retriable


def test_error_for_keeps_the_legacy_kinds_and_adds_codes():
    e = error_for(404)
    assert e.type == "HTTPStatus" and e.code == "fetch.http_status" and e.remedy == "none"
    assert error_for(503).remedy == "retry" and error_for(503).retriable
    t = error_for(0, "boom")
    assert t.type == "TransportError" and t.code == "fetch.transport" and t.retriable


def test_problem_details_shape():
    inner = make("fetch.http_status", "HTTP 500", status_code=500, retriable=True)
    err = make("remote.failed", "service said no", cause=inner, op="execute")
    p = err.problem(instance="/execute")
    assert p["type"] == "urn:webclient:error:remote.failed" and p["title"] == "Remote call failed"
    assert p["status"] == 502 and p["detail"] == "service said no" and p["instance"] == "/execute"
    assert p["remedy"] == "retry" and p["op"] == "execute" and p["kind"] == "RemoteError"
    assert p["cause"]["code"] == "fetch.http_status" and p["cause"]["status"] == 500
    # round-trips over the wire as a plain WebError
    assert WebError(**err.model_dump()).cause.code == "fetch.http_status"


def test_bound_only_fills_empty_fields():
    e = make("select.no_match", op="attr").bound(op="select", subject="doc:9")
    assert e.op == "attr" and e.subject == "doc:9"


def test_select_miss_carries_the_code_and_subject(httpserver):
    httpserver.expect_request("/s").respond_with_data("<html><body><p>x</p></body></html>",
                                                      content_type="text/html")
    with WebClient() as wc:
        doc = wc.fetch(httpserver.url_for("/s"))
        with pytest.raises(LookupError) as info:
            doc.select(".nope")
        assert info.value.error.code == "select.no_match" and info.value.error.remedy == "fix_selector"
        miss = doc.select(".nope", optional=True)
        assert miss.error.code == "select.no_match" and miss.error.subject == doc.name
        with pytest.raises(LookupError) as info2:
            doc.select("p").attr("data-x")
        assert info2.value.error.code == "select.no_attribute"


def test_docs_are_generated_from_the_catalogue():
    import subprocess, sys
    root = pathlib.Path(webclient.__file__).parent.parent
    r = subprocess.run([sys.executable, str(root / "scripts/gen_docs.py"), "errors", "--check"],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
