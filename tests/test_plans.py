"""Plans that do I/O: fan-out, per-row resolves, and recovery."""
import pytest

from webclient import (DROP_ROW, NULL, RAISE_ERROR, ResolveError, doc, el,
                       err, field, ref)


def test_follow_links_and_recover(site, wc):
    """EXAMPLES.md §8: fan out, follow each link, and when a detail page
    fails keep what the failure told you."""
    plan = (
        ref(site.url_for("/cards")).resolve()
        .then(
            doc.select("h1").attr("text").alias("board"),
            rows=doc.select_all(".card").map(
                el.select("h3").attr("text").alias("title"),
                link=el.select("a").attr("href"),
                detail=field("link").resolve().then(
                    salary=doc.query("salary"),
                    company=doc.query("company"),
                ).otherwise(
                    status=doc.status_code,
                    message=err.message,
                ),
            ),
        )
        .otherwise(RAISE_ERROR)
    )
    result = plan.collect(client=wc)
    # a sentinel does not tag — only a recovery projection produces `ok`
    assert "ok" not in result
    rows = result["rows"]
    assert [r["title"] for r in rows] == ["X1 Carbon", "T14", "Mystery"]

    assert rows[0]["detail"]["ok"] is True
    assert rows[0]["detail"]["salary"] == "10k"

    failed = rows[2]["detail"]
    assert failed["ok"] is False
    assert failed["status"] == 404
    assert "404" in failed["message"]


def test_rooted_plan_needs_no_context(site, wc):
    plan = ref(site.url_for("/cards")).resolve().select("h1").attr("text")
    assert plan.collect(client=wc).get() == "Laptops"


def test_plan_rooted_at_a_document_id(site, wc):
    document = wc.resolve(site.url_for("/cards"))
    plan = doc(document.id).select("h1").attr("text")
    assert plan.collect(client=wc).get() == "Laptops"


def test_drop_row_removes_failures(site, wc):
    plan = ref(site.url_for("/cards")).resolve().select_all(".card").map(
        el.select("h3").attr("text").alias("title"),
        detail=field_link().otherwise(DROP_ROW),
    )
    rows = plan.collect(client=wc)
    assert [r["title"] for r in rows] == ["X1 Carbon", "T14"]


def field_link():
    return el.select("a").attr("href").resolve().then(
        salary=doc.query("salary"))


def test_fanout_is_bounded(site, wc):
    wc.pool.max_http = 2
    plan = ref(site.url_for("/cards")).resolve().select_all(".card").map(
        el.select("h3").attr("text").alias("title"),
        detail=field_link().otherwise(NULL),
    )
    rows = plan.collect(client=wc)
    assert len(rows) == 3
    assert wc.stats().http_held == 0        # every lease returned


def test_plan_json_round_trip_runs_identically(site, wc):
    from webclient import Plan
    from webclient.execute import collect_plan
    plan = ref(site.url_for("/cards")).resolve().select_all(".card").map(
        el.select("h3").attr("text").alias("title"))
    direct = [dict(r) for r in plan.collect(client=wc)]
    restored = Plan.model_validate_json(plan.to_plan().model_dump_json())
    assert [dict(r) for r in collect_plan(restored, client=wc)] == direct


def test_telemetry_is_per_document(site, wc):
    seen = []
    wc.on(type(next(iter([]), None)) or object, lambda r: None)
    from webclient import RequestRecord
    wc.on(RequestRecord, seen.append)
    wc.resolve(site.url_for("/cards"))
    assert [r.status for r in seen] == [200]
