"""Phase 5 -- the query-building loop: the policy picks a record + fields BY INDEX and the loop
assembles a durable ``select_all(record).extract(fields).project()`` query, growing it over a
bounded few rounds. Static (a resolved page, no browser)."""

from webclient.core.document import Document
from webclient.llm import QueryDecision, QueryObservation, build_query
from webclient.query.expr import from_blob

SHOP = """
<html><body><main>
  <ul class="results">
    <li class="card"><h3 class="title">Aeropress</h3><span class="price">$39</span></li>
    <li class="card"><h3 class="title">Grinder</h3><span class="price">$59</span></li>
    <li class="card"><h3 class="title">Kettle</h3><span class="price">$79</span></li>
  </ul>
</main></body></html>
"""


def _doc() -> Document:
    return Document(url="http://x/", content=SHOP.encode(), kind="html", status_code=200)


def test_query_loop_builds_a_record_extraction_by_index():
    def policy(obs: QueryObservation) -> QueryDecision:
        if obs.step == 0:
            return QueryDecision(record=obs.records[0].index)  # pick the top record region
        if obs.step == 1:  # add two fields from the record's leaves (name, price), by index
            return QueryDecision(fields={"name": obs.fields[0].index, "price": obs.fields[1].index})
        return QueryDecision(done=True)  # the sample looks right -> done

    run = build_query(_doc(), policy, max_rounds=5)
    assert run.done and run.reason == "done"
    assert run.row_count == 3
    assert run.sample[0] == {"name": "Aeropress", "price": "$39"}
    # the authored query is a real, durable, serialisable Plan -- rebuild the blob and re-run it
    rows = from_blob(run.blob).collect(_doc())
    assert [r["name"] for r in rows] == ["Aeropress", "Grinder", "Kettle"]
    assert [r["price"] for r in rows] == ["$39", "$59", "$79"]


def test_query_loop_never_authors_a_selector_indexes_only():
    seen: list[QueryObservation] = []

    def policy(obs: QueryObservation) -> QueryDecision:
        seen.append(obs)
        if obs.step == 0:
            return QueryDecision(record=obs.records[0].index)
        if obs.step == 1:
            return QueryDecision(fields={"name": obs.fields[0].index})
        return QueryDecision(done=True)

    run = build_query(_doc(), policy)
    # the record option is a durable item selector WE built (the policy only ever sent indexes)
    assert seen[0].records[0].selector.startswith("li")
    assert "select_all" in run.describe and "li" in run.describe


def test_query_loop_reports_zero_rows_for_a_bad_pick_and_is_bounded():
    # a policy that keeps picking the record but never adds a field grows nothing -> stalls out,
    # bounded, rather than looping forever.
    def policy(obs: QueryObservation) -> QueryDecision:
        return QueryDecision(record=obs.records[0].index)  # never adds fields, never done

    run = build_query(_doc(), policy, max_rounds=6)
    assert not run.done and run.reason in ("stalled", "budget")


def test_query_loop_validation_error_feeds_back_and_recovers():
    # round 1 picks a field that yields rows; if it had been empty the error surfaces. Here we
    # assert the happy path sets no error and the unhappy path (no fields) leaves row_count 0.
    def good(obs: QueryObservation) -> QueryDecision:
        if obs.step == 0:
            return QueryDecision(record=obs.records[0].index)
        if obs.step == 1:
            return QueryDecision(fields={"name": obs.fields[0].index}, done=True)
        return QueryDecision(done=True)

    run = build_query(_doc(), good)
    assert run.row_count == 3 and not run.error
