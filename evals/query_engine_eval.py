"""Deterministic eval of the index-based query author's CEILING.

The onboarding pipeline can author extraction queries two ways
(``webclient.pipelines.onboarding.write_query(author_engine=...)``):

  * ``"text"``  -- the model writes a ``wq.doc...project()`` CSS/XPath query chain.
  * ``"index"`` -- the model only picks a RECORD number and FIELD numbers from
    ``element_index.record_options`` / ``field_options``; ``llm.query_agent.build_query``
    turns those indexes into a durable ``select_all(rec).extract(**cols).project()`` query,
    where every column is ``select(field_selector).attr("text")``.

The index engine can only produce what the PRIMITIVE surfaces, and only in that fixed
shape (flat columns, text only). So its ceiling -- the best it could do with a PERFECT
policy -- is a property of the primitive, testable with no LLM. This harness measures that
ceiling over a corpus of representative pages (the messy_html scenarios plus a few clean
shapes), by running an ORACLE policy through the real ``build_query`` and by inspecting
``record_options`` / ``field_options`` directly.

Run:  env/bin/python evals/query_engine_eval.py
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# the corpus lives with the tests; import it read-only.
_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO / "tests"))

from webclient.core.document import Document  # noqa: E402
from webclient.core.document.html import tree  # noqa: E402
from webclient.core.document.element_index import field_options, record_options  # noqa: E402
from webclient.interface import wq  # noqa: E402
from webclient.llm.query_agent import QueryDecision, QueryObservation, build_query  # noqa: E402

import extra_shapes  # noqa: E402  (evals/extra_shapes.py -- clean corpus shapes)
from messy_html import all_scenarios  # noqa: E402


# --------------------------------------------------------------------------- #
# corpus assembly
# --------------------------------------------------------------------------- #
@dataclass
class Page:
    """One corpus page: the entry HTML/JSON/XML + the brief fields + the expected rows."""

    name: str
    html: str
    kind: str  # html | xml | json
    fields: list[str]  # required leaf fields (dotted); trailing '?' = optional
    expected: "list[dict] | str"  # rows, or "EMPTY"
    notes: str = ""


def _kind_for(entry: str, content_types: dict) -> str:
    ct = content_types.get(entry, "")
    if "json" in ct:
        return "json"
    if "xml" in ct or "rss" in ct or entry.endswith(".xml"):
        return "xml"
    return "html"


def corpus() -> "list[Page]":
    pages: list[Page] = []
    for sc in all_scenarios():
        pages.append(Page(
            name=sc.name,
            html=sc.pages[sc.entry],
            kind=_kind_for(sc.entry, sc.content_types),
            fields=list(sc.fields),
            expected=sc.expected,
            notes=sc.notes,
        ))
    pages.extend(extra_shapes.pages())
    return pages


# --------------------------------------------------------------------------- #
# expected-row helpers
# --------------------------------------------------------------------------- #
def _clean(f: str) -> str:
    return f[:-1] if f.endswith("?") else f


def _required(fields: "list[str]") -> "list[str]":
    return [_clean(f) for f in fields if not f.endswith("?")]


def _dig(row: dict, dotted: str) -> Any:
    """The value of a dotted path in an expected row (nested dicts)."""
    node: Any = row
    for part in dotted.split("."):
        if isinstance(node, dict) and part in node:
            node = node[part]
        else:
            return None
    return node


def _expected_col(expected: "list[dict]", dotted: str) -> "list[Any]":
    return [_dig(r, dotted) for r in expected]


# --------------------------------------------------------------------------- #
# primitive inspection
# --------------------------------------------------------------------------- #
def _collect(expr: Any, doc: Any) -> "list[dict]":
    try:
        rows = expr.collect(doc)
    except Exception as exc:  # noqa: BLE001
        return [{"__error__": str(exc)}]
    return [r for r in rows if isinstance(r, dict)]


def _record_rank(root: Any, doc: Any, expected: "list[dict] | str") -> "tuple[int, list]":
    """(rank, options): the 1-based rank of the FIRST record option whose row count matches
    the expected record count (0 == none matched); plus the option list. For an EMPTY-expected
    page any surfaced record region is a false positive, so rank is 0 unless no region at all."""
    opts = record_options(root)
    if expected == "EMPTY":
        # correct answer is "no dataset region here"; a surfaced region is a decoy hit.
        return (1 if not opts else 0), opts
    want = len(expected)
    for i, o in enumerate(opts, start=1):
        rows = _collect(wq.doc.select_all(o.selector).extract(_=wq.doc.attr("text")).project(), doc)
        if len(rows) == want:
            return i, opts
    return 0, opts


def _oracle_field(root: Any, doc: Any, rec_sel: str, dotted: str,
                  expected: "list[dict]") -> "tuple[int | None, bool]":
    """Best field option (by index) for a required field: the one whose per-row
    ``select(sel).attr('text')`` column reproduces the expected column exactly. Returns
    (index or None, exact) where exact means a perfect column match was found."""
    want = _expected_col(expected, dotted)
    want_str = [None if v is None else str(v) for v in want]
    best: "tuple[int | None, bool]" = (None, False)
    for f in field_options(root, rec_sel):
        col_expr = wq.doc.select_all(rec_sel).extract(
            c=wq.doc.select(f.selector).attr("text")).project()
        rows = _collect(col_expr, doc)
        got = [r.get("c") for r in rows]
        if got == want_str and all(v not in (None, "") for v in got):
            return f.index, True
        # partial: same length and majority match -> remember as a fallback
        if len(got) == len(want_str):
            hits = sum(1 for a, b in zip(got, want_str) if a == b)
            if hits and best[0] is None:
                best = (f.index, False)
    return best


# --------------------------------------------------------------------------- #
# the oracle build_query run (the real index engine, perfect brain)
# --------------------------------------------------------------------------- #
def _oracle_policy(root: Any, doc: Any, page: Page, plan: dict) -> Any:
    """A policy that plays the index engine PERFECTLY from a precomputed plan
    (record index + per-field indexes), so ``build_query`` measures the engine's ceiling."""
    def policy(obs: QueryObservation) -> QueryDecision:
        if obs.step == 0 and plan.get("record"):
            return QueryDecision(record=plan["record"])
        if plan.get("fields"):
            f = plan.pop("fields")
            return QueryDecision(fields=f, done=True)
        return QueryDecision(done=True)

    return policy


# --------------------------------------------------------------------------- #
# scoring
# --------------------------------------------------------------------------- #
@dataclass
class Result:
    name: str
    kind: str
    verdict: str = "FAIL"
    record_rank: int = 0
    n_required: int = 0
    n_covered: int = 0
    exact: bool = False
    engine_rows: int = 0
    expected_rows: int = 0
    reason: str = ""


def evaluate(page: Page) -> Result:
    res = Result(name=page.name, kind=page.kind)
    req = _required(page.fields)
    res.n_required = len(req)
    exp = page.expected
    res.expected_rows = 0 if exp == "EMPTY" else len(exp)

    if page.kind == "json":
        # a JSON body has no DOM; record_options / field_options are CSS-over-lxml, and the index
        # engine's fixed select().attr('text') cannot dotted-path into a JSON structure.
        doc = Document(url="http://x/", content=page.html.encode(), kind="json", status_code=200)
        opts = record_options(tree(doc))
        res.reason = ("JSON source -- the index primitive is CSS-over-lxml; it cannot dotted-path "
                      f"into JSON (record_options surfaced {len(opts)} region[s])")
        res.verdict = "FAIL"
        return res

    # html AND xml both have an lxml DOM the primitive can address (xml -> the xml parser, so
    # <item>/<pubDate> records are reachable); only the Document kind differs.
    doc = Document(url="http://x/", content=page.html.encode(), kind=page.kind, status_code=200)
    root = tree(doc)

    rank, opts = _record_rank(root, doc, exp)
    res.record_rank = rank

    if exp == "EMPTY":
        # correct output is 0 rows; the engine ALWAYS emits rows once it picks a region + field.
        if rank == 1:
            res.verdict, res.reason = "PASS", "no record region surfaced (correct: 0 rows)"
        else:
            res.verdict = "FAIL"
            res.reason = (f"a decoy region was surfaced (rank {rank}); the engine would extract "
                          "rows where the correct answer is EMPTY")
        return res

    if rank == 0:
        if opts:
            got = len(_collect(wq.doc.select_all(opts[0].selector).extract(
                _=wq.doc.attr("text")).project(), doc))
            res.reason = (f"no surfaced region matches {res.expected_rows} records; top option "
                          f"'{opts[0].selector}' selects {got} (region reports {opts[0].repeats})")
        else:
            res.reason = f"no record region surfaced at all (want {res.expected_rows})"
        res.verdict = "FAIL"
        return res

    rec_sel = opts[rank - 1].selector
    # oracle-assign each required field
    plan_fields: dict[str, int] = {}
    covered = 0
    misses: list[str] = []
    for fld in req:
        idx, exact = _oracle_field(root, doc, rec_sel, fld, exp)
        if idx is not None and exact:
            plan_fields[fld] = idx
            covered += 1
        else:
            if idx is not None:
                plan_fields[fld] = idx
            misses.append(fld)
    res.n_covered = covered

    # run the REAL engine with the oracle plan
    plan = {"record": rank, "fields": dict(plan_fields)}
    run = build_query(doc, _oracle_policy(root, doc, page, plan), max_rounds=4)
    res.engine_rows = run.row_count
    exact_rows = run.sample == [{k: (None if v is None else str(v)) for k, v in
                                 {f: _dig(r, f) for f in req}.items()} for r in exp[:5]]
    res.exact = exact_rows and run.row_count == len(exp)

    if res.exact:
        res.verdict = "PASS"
        res.reason = "oracle index query reproduces expected rows exactly"
    elif covered == len(req) and run.row_count == len(exp):
        res.verdict = "PARTIAL"
        res.reason = ("every required field's VALUES are reachable via .attr('text'), but the "
                      "output shape differs (nesting/keys)" if any("." in f for f in req)
                      else "fields reachable but row set differs")
    elif covered:
        res.verdict = "PARTIAL"
        res.reason = f"{covered}/{len(req)} required fields reachable; misses: {', '.join(misses)}"
    else:
        res.verdict = "FAIL"
        res.reason = (f"record picked (rank {rank}, {run.row_count} rows) but no required field "
                      f"extractable via .attr('text'): {', '.join(misses)}")
    return res


# --------------------------------------------------------------------------- #
# report
# --------------------------------------------------------------------------- #
def main() -> None:
    results = [evaluate(p) for p in corpus()]
    width = max(len(r.name) for r in results)
    print("\n=== Index-engine ceiling: per-page deterministic results ===\n")
    print(f"{'page':<{width}}  {'kind':<5} {'verdict':<8} {'rec#':>4} {'cover':>6} {'rows':>9}  reason")
    print("-" * (width + 60))
    for r in results:
        cover = f"{r.n_covered}/{r.n_required}" if r.n_required else "-"
        rows = f"{r.engine_rows}/{r.expected_rows}"
        print(f"{r.name:<{width}}  {r.kind:<5} {r.verdict:<8} {r.record_rank:>4} {cover:>6} "
              f"{rows:>9}  {r.reason}")

    n = len(results)
    npass = sum(r.verdict == "PASS" for r in results)
    npart = sum(r.verdict == "PARTIAL" for r in results)
    nfail = sum(r.verdict == "FAIL" for r in results)
    print("\n=== Coverage ===")
    print(f"  pages:   {n}")
    print(f"  PASS:    {npass} ({npass / n:.0%})")
    print(f"  PARTIAL: {npart} ({npart / n:.0%})")
    print(f"  FAIL:    {nfail} ({nfail / n:.0%})")
    # record-detection sub-metric (html, non-empty only)
    html_nonempty = [r for r in results if r.kind in ("html", "xml") and r.expected_rows > 0]
    top1 = sum(r.record_rank == 1 for r in html_nonempty)
    top2 = sum(1 <= r.record_rank <= 2 for r in html_nonempty)
    print("\n=== Record detection (HTML dataset pages) ===")
    print(f"  pages:            {len(html_nonempty)}")
    print(f"  correct region @ rank 1: {top1} ({top1 / len(html_nonempty):.0%})")
    print(f"  correct region @ rank 1-2: {top2} ({top2 / len(html_nonempty):.0%})")


if __name__ == "__main__":
    main()
