"""The safe query compiler -- rebuild a model's ``wq`` chain WITHOUT eval, and root it at a URL.

Covers: a written chain rebuilds and runs; fences / prose / smart quotes are tolerated; a
prompt-injected line reaching ``__globals__`` / ``__class__`` / a non-``wq`` name / a stray
statement is REFUSED; and reroot prepends ``reference(url).resolve()`` into one self-contained blob.
"""

from __future__ import annotations

import asyncio
from typing import cast

import pytest
from pytest_httpserver import HTTPServer
from web.onboard.compile import QueryError, parse_query, query_code, reroot
from web.resolve import Resolver


def _run(coro: object) -> object:
    return asyncio.run(cast("asyncio.Future[object]", coro))


def test_parse_and_reroot_runs(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/p").respond_with_data(
        b"<ul><li class='row'><span class='n'>Ada</span></li>"
        b"<li class='row'><span class='n'>Bo</span></li></ul>",
        content_type="text/html",
    )
    chain = parse_query(
        'wq.doc.select_all("li.row").extract(n=wq.doc.select(".n").attr("text"))'
    )
    query = reroot(chain, httpserver.url_for("/p"))
    assert (
        query.to_blob().count("reference") == 0
    )  # source lives in the blob, not a literal call
    rows = cast("list[dict[str, object]]", _run(query.acollect()))
    assert rows == [{"n": "Ada"}, {"n": "Bo"}]


def test_query_code_strips_fence_prose_and_smart_quotes() -> None:
    reply = "Here is the query:\n```python\nquery = wq.doc.select_all(“.row”).extract()\n```"
    code = query_code(reply)
    assert (
        code == 'wq.doc.select_all(".row").extract()'
    )  # starts at wq., ASCII quotes, no fence


@pytest.mark.parametrize(
    "hostile",
    [
        "wq.reference('u').__globals__['os']",  # reach the module globals
        "wq.doc.__class__.__mro__",  # reach the type / builtins
        "os.system('rm -rf /')",  # a non-wq name
        "wq.doc.select_all(*['a'])",  # starred args
        "__import__('os').system('x')",  # a builtin call
        "1 + 1",  # a stray expression
        "",  # nothing
    ],
)
def test_parse_refuses_hostile_or_empty(hostile: str) -> None:
    with pytest.raises(QueryError):
        parse_query(hostile)


def test_reroot_leaves_a_self_contained_chain_alone(httpserver: HTTPServer) -> None:
    # a chain the model already rooted at reference(...) must not be double-rooted
    url = httpserver.url_for("/x")
    chain = parse_query(f'wq.reference({url!r}).resolve().select_all(".row")')
    out = reroot(chain, "http://other/")
    assert url in out.to_blob() and "other" not in out.to_blob()
