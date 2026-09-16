"""Deterministic proof that every messy-HTML scenario is SOLVABLE with the DSL.

For each :class:`~tests.messy_html.Scenario` we serve its pages locally, run the known-good
``solution`` query THROUGH the pipeline's own loader (:func:`_parse_query` +
:func:`_executable_query`, the exact path a model-authored query takes), and assert the rows
equal ``expected``. This locks in that the hard shapes the LLM eval then probes -- flat
sibling runs, regex value/unit split, empty-vs-archived events, an SPA false flag, a nested
per-record resolve, an RSS/XML feed, a JSON API, and an XHR-behind-a-shell -- are all things
the query engine can actually express. The separate LLM eval (``scripts/query_eval.py``)
checks whether the model FINDS these queries on its own; this file checks they EXIST.
"""

from __future__ import annotations

import pytest

from webclient import Document, WebClient
from webclient.pipelines.onboarding import (
    _data_rows,
    _executable_query,
    _parse_query,
)
from webclient.signals import flags_from_response
from webclient.signals.dom import _is_data_endpoint

from messy_html import all_scenarios, spa_false_flag, xhr_behind_shell


def _serve(httpserver, sc) -> str:
    """Register a scenario's pages (with their content types) and return the entry URL."""
    for path, body in sc.pages.items():
        ctype = sc.content_types.get(path, "text/html; charset=utf-8")
        httpserver.expect_request(path).respond_with_data(body, content_type=ctype)
    return httpserver.url_for(sc.entry)


def _rows_of(result) -> list:
    return [r.model_dump() if hasattr(r, "model_dump") else r for r in _data_rows(result)]


def _normalize(expected, base: str) -> list:
    """Fill the ``https://SITE`` placeholder in expected rows with the live base URL."""
    def fix(v):
        if isinstance(v, str):
            return v.replace("https://SITE", base)
        if isinstance(v, dict):
            return {k: fix(x) for k, x in v.items()}
        return v
    return [fix(r) for r in expected]


@pytest.mark.parametrize("sc", all_scenarios(), ids=lambda s: s.name)
def test_scenario_solution_extracts_expected(sc, httpserver):
    # the solution loads through the SAME path a model reply takes (parse -> executable),
    # so this also proves the loader accepts the query shapes we want the model to write.
    entry = _serve(httpserver, sc)
    base = httpserver.url_for("/").rstrip("/")
    with WebClient() as wc:
        doc_expr = _parse_query(sc.solution)
        exe = _executable_query(doc_expr, entry, None)
        rows = _rows_of(exe.collect())

    if sc.expected == "EMPTY":
        assert rows == [], f"{sc.name}: expected 0 rows, got {rows!r}"
    else:
        assert rows == _normalize(sc.expected, base), sc.name


def test_spa_false_flag_is_not_escalated():
    # the Adobe-style false flag: framework markers fire, but the served HTML already holds
    # the content, so the static contra evidence pulls spa BELOW present -- no needless
    # browser escalation, and a plain static query (proven above) suffices.
    sc = spa_false_flag()
    body = sc.pages["/"].encode()
    f = flags_from_response(200, {"content-type": "text/html"}, {}, body)
    assert not f["spa"].present  # contra wins
    assert any(s.contra for s in f["spa"].signals)


def test_xhr_shell_flags_spa():
    # the shell page (empty root + bundle) MUST flag spa so auto renders it and can then
    # discover the real data endpoint.
    sc = xhr_behind_shell()
    body = sc.pages["/"].encode()
    assert flags_from_response(200, {"content-type": "text/html"}, {}, body)["spa"].present


def test_render_surfaces_the_xhr_data_endpoint_when_the_shell_is_selected():
    # THE question: when the main page is selected and rendered, does the pipeline CATCH the
    # XHR calls -- and tell the records API apart from the analytics beacon? We stand in for a
    # render by attaching the captured network events to the document, exactly as the browser
    # backing does, then read them back off the selected document.
    from webclient.core.document.live import network_event

    sc = xhr_behind_shell()
    doc = Document(url="https://acme.example/", kind="html", content=sc.pages["/"].encode(),
                   status_code=200)
    for url in sc.xhr_endpoints:
        full = url if url.startswith("http") else f"https://acme.example{url}"
        doc.events.append(network_event("GET", full, "xhr", doc))

    caught = {c.url for c in doc.xhr_endpoints()}
    # every XHR the render made is surfaced on the selected document...
    assert any(u.endswith("/api/posts") for u in caught)
    assert any("google-analytics" in u for u in caught)
    # ...and the records API is told apart from the analytics beacon.
    assert _is_data_endpoint("https://acme.example/api/posts")
    assert not _is_data_endpoint("https://www.google-analytics.com/collect")
