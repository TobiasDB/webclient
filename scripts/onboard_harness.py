"""Run the onboarding pipeline over a MATRIX of briefs x companies -- in PARALLEL, end to
end, using the local Claude Code model (no paid API key) and CURATED seed URLs (no live web
search, so we don't get rate-limited / blocked).

Each company runs in its OWN subprocess (true isolation + parallelism). For each we capture:
  harness_runs/<ts>/<slug>.log   -- the FULL pipeline trace (search/verify, crawl page-by-
                                     page with transport+signals, candidate eval, query
                                     authoring + retries, the review gates + diagnosis)
  harness_runs/<ts>/<slug>.json  -- the structured result: ok/reason, the authored query
                                     (+sample), and every stage Review (verdict/passed/
                                     issues/summary) incl. the failure diagnosis
So a reviewer (you, or an agent) can read exactly what went wrong per company and collect
them to decide fixes. `--aggregate` adds one cross-matrix "what went wrong / how to fix".

    env/bin/python scripts/onboard_harness.py --parallel 10            # the whole matrix
    env/bin/python scripts/onboard_harness.py --brief ir-news --parallel 5
    env/bin/python scripts/onboard_harness.py --only "Books to Scrape" # one case
    env/bin/python scripts/onboard_harness.py --parallel 10 --aggregate

NOTE (yours to weigh): every model call goes through your Claude Code subscription via
`claude -p` -- it draws on your plan's usage/rate limits and is slow (a company is dozens
of calls). 10 in parallel = up to 10 concurrent `claude -p` processes.
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import datetime as dt
import json
import logging
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from importlib.resources import files

from webclient import WebClient
from webclient.pipelines import Brief, SearchHit, onboard_company


def _packaged_brief(name: str) -> Brief:
    """Load a brief shipped under webclient/pipelines/briefs/<name>.md."""
    return Brief.from_markdown(
        files("webclient.pipelines").joinpath(f"briefs/{name}.md").read_text(encoding="utf-8")
    )

# ---------------------------------------------------------------------------- #
# The matrix: reusable briefs + curated companies with their seed URL(s).
# ---------------------------------------------------------------------------- #

BRIEFS: dict[str, Brief] = {
    "product-catalogue": Brief(
        name="product-catalogue", title="Product catalogue",
        description="every product/item the page lists, each with its name and price",
        fields=["name", "price", "url?", "rating?"],
    ),
    "quotes": Brief(
        name="quotes", title="Quotes",
        description="every quote listed on the page, each with its text, author and tags",
        fields=["text", "author", "tags?"],
    ),
    "countries": Brief(
        name="countries", title="Countries",
        description="every country listed, each with its name, capital, population and area",
        fields=["name", "capital", "population?", "area?"],
    ),
    "ir-news": _packaged_brief("ir-news"),
}

# (brief key, company, [seed urls]) -- curated so no live search runs.
CASES: list[tuple[str, str, list[str]]] = [
    ("product-catalogue", "Books to Scrape", ["https://books.toscrape.com/"]),
    ("product-catalogue", "WebScraper Test Shop", ["https://webscraper.io/test-sites/e-commerce/allinone"]),
    ("quotes", "Quotes to Scrape", ["https://quotes.toscrape.com/"]),
    ("countries", "Scrape This Site", ["https://www.scrapethissite.com/pages/simple/"]),
    ("ir-news", "Adobe", ["https://www.adobe.com/investor-relations/investor-news.html"]),
    ("ir-news", "Palo Alto Networks", ["https://investors.paloaltonetworks.com/news-releases"]),
    ("ir-news", "Salesforce", ["https://investor.salesforce.com/press-releases/default.aspx"]),
    ("ir-news", "ServiceNow", ["https://www.servicenow.com/company/media/press-room.html"]),
    ("ir-news", "Snowflake", ["https://investors.snowflake.com/news/default.aspx"]),
    ("ir-news", "Datadog", ["https://investors.datadoghq.com/news-releases/default.aspx"]),
]


def _slug(brief_key: str, company: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", f"{company}-{brief_key}".lower()).strip("-")


def _canned_search(urls: list[str], company: str):
    """A SearchFn that ignores the query and hands back the curated seed URLs -- no live
    web search runs, so nothing gets blocked."""
    def search(query: str, k: int = 6) -> list[SearchHit]:
        return [SearchHit(url=u, title=company, snippet=f"{company} (curated seed)") for u in urls]
    return search


def _result_dict(brief_key: str, r) -> dict:
    """The structured, JSON-serialisable record a reviewer reads."""
    return {
        "company": r.company, "brief": brief_key, "ok": r.ok, "reason": r.reason,
        "cost_usd": r.cost_usd,
        "source": (r.evaluation.url if r.evaluation else None),
        "query": None if r.query is None else {
            "describe": r.query.describe, "row_count": r.query.row_count,
            "tested": r.query.tested, "sample": list(r.query.sample)[:5], "blob": r.query.blob,
        },
        "reviews": [
            {"stage": v.stage, "passed": v.passed, "verdict": v.verdict,
             "score": v.score, "issues": v.issues, "summary": v.summary}
            for v in r.reviews
        ],
        "trace": r.steps,
    }


# ---------------------------------------------------------------------------- #
# worker: run ONE case in its own process; logs -> stdout, result -> <prefix>.json
# ---------------------------------------------------------------------------- #

def _md_table(sample: list) -> str:
    """A markdown table from a list of row dicts (or a bullet list for scalars)."""
    dicts = [r for r in sample if isinstance(r, dict)]
    if not dicts:
        return "\n".join(f"- {r}" for r in sample) or "_(no rows)_"
    cols: list[str] = []
    for r in dicts:
        for k in r:
            if k not in cols:
                cols.append(k)
    head = "| " + " | ".join(cols) + " |\n| " + " | ".join("---" for _ in cols) + " |"
    body = "\n".join(
        "| " + " | ".join(str(r.get(c, "")).replace("|", "\\|")[:60] for c in cols) + " |"
        for r in dicts
    )
    return head + "\n" + body


def _report_md(rec: dict, slug: str, blob_path: str) -> str:
    """A human-readable report for one company: outcome, the authored query, a sample
    table, the reviews, and a COPY-PASTE snippet to run the query yourself."""
    q = rec.get("query")
    mark = "✅" if rec["ok"] else "❌"
    out = [
        f"# {mark} {rec['company']} × {rec['brief']}", "",
        f"- **Result:** {'ready' if rec['ok'] else 'not onboarded — ' + rec['reason']}",
        f"- **Source:** {rec.get('source') or '—'}",
        f"- **Rows extracted:** {q['row_count'] if q else 0}",
        f"- **Full trace:** [`{slug}.log`](./{slug}.log)   ·   **Re-run:** "
        f"`env/bin/python scripts/onboard_harness.py --only \"{rec['company']}\"`",
        "",
    ]
    if q:
        out += [
            "## Authored query", "", f"```\n{q['describe']}\n```", "",
            "### Test it yourself",
            "The query blob is self-contained (fetch + extract). Run it and see the rows:", "",
            "```bash", "env/bin/python - <<'PY'",
            "from webclient import WebClient, from_blob",
            f'blob = open("{blob_path}").read()',
            "with WebClient() as wc:",
            "    for row in from_blob(blob, wc).collect()[:20]:",
            "        print(row)", "PY", "```", "",
            f"### Sample rows ({q['row_count']} total, tested={q['tested']})", "",
            _md_table(q["sample"]), "",
        ]
    else:
        out += ["## No query authored", "", f"Reason: **{rec['reason']}** — see the trace log.", ""]
    if rec["reviews"]:
        out += ["## Review (the gates + diagnosis)", ""]
        for v in rec["reviews"]:
            m = "" if v["stage"] == "failure" else ("✓ " if v["passed"] else "✗ ")
            grade = f" ({v['verdict']}{', ' + str(v['score']) + '/10' if v['score'] else ''})" if v["verdict"] else ""
            out.append(f"**{m}{v['stage']}{grade}** — {v['summary']}")
            out += [f"  - {i}" for i in v["issues"]]
            out.append("")
    return "\n".join(out)


def _write_index(records: list[dict], outdir) -> None:
    lines = [
        f"# Onboarding harness run — {outdir.name}", "",
        "Each row links to a human-readable report (outcome, the authored query, a sample "
        "table, the reviews, and a snippet to run the query yourself) and the full trace log.", "",
        "| | company | brief | rows | reports |",
        "|---|---|---|---|---|",
    ]
    for r in records:
        rows = (r.get("query") or {}).get("row_count", 0)
        slug = r["slug"]
        lines.append(f"| {'✅' if r['ok'] else '❌'} | {r['company']} | {r['brief']} | {rows} | "
                     f"[report](./{slug}.md) · [trace](./{slug}.log) · [json](./{slug}.json) |")
    ok = sum(1 for r in records if r["ok"])
    lines += ["", f"**{ok}/{len(records)} onboarded.**  Full matrix: `summary.json`. "
              "Cross-cutting fixes (if `--aggregate`): `aggregate.md`.", ""]
    (outdir / "index.md").write_text("\n".join(lines))


def _run_worker(a: argparse.Namespace) -> int:
    from claude_llm_adapter import claude_code_llm  # local to the worker process

    # the full trace to stdout (captured by the parent into the .log file)
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(logging.Formatter("%(message)s"))
    lg = logging.getLogger("webclient.pipelines")
    lg.handlers[:] = [h]
    lg.setLevel(logging.DEBUG if a.verbose else logging.INFO)
    lg.propagate = False

    brief = BRIEFS[a.brief_key]
    urls = a.urls.split(",")
    with WebClient() as wc:
        r = onboard_company(
            a.company, brief, wc=wc, llm=claude_code_llm,
            search=_canned_search(urls, a.company),
            browser=not a.no_browser, review=not a.no_review,
        )
    Path(a.out).write_text(json.dumps(_result_dict(a.brief_key, r), indent=2, default=str))
    return 0 if r.ok else 1


# ---------------------------------------------------------------------------- #
# parent: fan the cases out across processes, collect, summarise
# ---------------------------------------------------------------------------- #

def _load_existing(outdir: Path, case: tuple[str, str, list[str]]) -> "dict | None":
    """A finished case's record from a prior run (for --resume), or None if not done."""
    slug = _slug(case[0], case[1])
    p = outdir / f"{slug}.json"
    if not p.exists():
        return None
    try:
        rec = json.loads(p.read_text())
    except Exception:
        return None
    rec.setdefault("slug", slug)
    rec.setdefault("secs", 0.0)
    return rec


def _launch(case: tuple[str, str, list[str]], outdir: Path, flags: list[str]) -> dict:
    brief_key, company, urls = case
    slug = _slug(brief_key, company)
    prefix = outdir / slug
    cmd = [sys.executable, os.path.abspath(__file__), "--worker",
           "--brief-key", brief_key, "--company", company, "--urls", ",".join(urls),
           "--out", str(prefix.with_suffix(".json"))] + flags
    t0 = time.monotonic()
    proc = subprocess.run(cmd, capture_output=True, text=True)
    prefix.with_suffix(".log").write_text((proc.stdout or "") + (proc.stderr or ""))
    secs = time.monotonic() - t0
    try:
        rec = json.loads(prefix.with_suffix(".json").read_text())
    except Exception:  # the worker crashed before writing -- synthesise a failure record
        rec = {"company": company, "brief": brief_key, "ok": False,
               "reason": f"worker crashed (exit {proc.returncode}); see {slug}.log",
               "query": None, "reviews": [], "trace": []}
    rec["secs"] = round(secs, 1)
    rec["slug"] = slug
    # a testable blob sidecar + a human-readable per-company report
    blob_path = prefix.with_suffix(".blob.json")
    if rec.get("query"):
        blob_path.write_text((rec["query"] or {}).get("blob", ""))
    prefix.with_suffix(".md").write_text(_report_md(rec, slug, str(blob_path)))
    rows = (rec.get("query") or {}).get("row_count", 0)
    print(f"  {'✓' if rec['ok'] else '✗'} {company} × {brief_key}  ({rows} rows, {secs:.0f}s)",
          file=sys.stderr)
    return rec


def _aggregate(records: list[dict], outdir: Path) -> None:
    """One cross-matrix review: feed every case's reason + reviews to the model and ask
    what the COMMON failure modes are and how to fix them."""
    from claude_llm_adapter import claude_code_llm

    lines = []
    for r in records:
        revs = "; ".join(f"{v['stage']}{'ok' if v['passed'] else 'FAIL'}: {v['summary']}" for v in r["reviews"])
        lines.append(f"- {r['company']} [{r['brief']}]: {'OK' if r['ok'] else 'FAIL'} — "
                     f"{r['reason'] or 'ok'} | rows={(r.get('query') or {}).get('row_count', 0)} | {revs}")
    prompt = (
        "These are the results of running a web-data onboarding pipeline over several "
        "companies. For each, the outcome, the failure reason, and the per-stage review "
        "diagnoses are given.\n\n" + "\n".join(lines) +
        "\n\nAcross ALL of these, what are the COMMON failure modes, and what concrete "
        "changes to the pipeline (search, crawl, selection, query authoring, rendering) "
        "would fix the most cases? Reply as a short prioritised markdown list."
    )
    md = claude_code_llm(prompt)
    (outdir / "aggregate.md").write_text(md)
    print("\n" + "=" * 80 + "\nAGGREGATE REVIEW (harness_runs/.../aggregate.md)\n" + "=" * 80)
    print(md)


def main() -> None:
    ap = argparse.ArgumentParser(description="parallel onboarding harness (curated URLs + Claude Code)")
    ap.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--brief-key"); ap.add_argument("--company"); ap.add_argument("--urls"); ap.add_argument("--out")
    ap.add_argument("--brief", action="append", help="only these brief key(s)")
    ap.add_argument("--only", action="append", help="only these compan(y/ies)")
    ap.add_argument("--parallel", type=int, default=4,
                    help="max companies at once (each may launch a browser; keep modest to avoid OOM)")
    ap.add_argument("--max", type=int, default=0, help="stop after N cases (0 = all)")
    ap.add_argument("--resume", metavar="DIR", help="reuse finished cases in DIR; run only the rest")
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--no-review", action="store_true")
    ap.add_argument("--verbose", action="store_true", help="worker logs at DEBUG")
    ap.add_argument("--aggregate", action="store_true", help="add a cross-matrix review at the end")
    a = ap.parse_args()

    if a.worker:
        sys.exit(_run_worker(a))

    cases = [c for c in CASES
             if (not a.brief or c[0] in a.brief) and (not a.only or c[1] in a.only)]
    if a.max:
        cases = cases[: a.max]
    outdir = Path(a.resume) if a.resume else Path("harness_runs") / dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    outdir.mkdir(parents=True, exist_ok=True)
    flags = (["--no-browser"] if a.no_browser else []) + (["--no-review"] if a.no_review else []) \
        + (["--verbose"] if a.verbose else [])

    records: list[dict] = []
    todo: list[tuple[str, str, list[str]]] = []
    for c in cases:  # --resume: keep finished cases, only run the missing ones
        done = _load_existing(outdir, c) if a.resume else None
        (records.append(done) if done is not None else todo.append(c))
    print(f"{len(cases)} case(s): {len(records)} reused, {len(todo)} to run, "
          f"{a.parallel} in parallel -> {outdir}", file=sys.stderr)

    with cf.ThreadPoolExecutor(max_workers=max(1, a.parallel)) as pool:
        futs = [pool.submit(_launch, c, outdir, flags) for c in todo]
        for f in cf.as_completed(futs):
            records.append(f.result())

    records.sort(key=lambda r: (r["brief"], r["company"]))
    (outdir / "summary.json").write_text(json.dumps(records, indent=2, default=str))
    _write_index(records, outdir)  # a readable index linking every per-company report

    print(f"\n{'='*96}\nHARNESS RESULTS  ->  {outdir}\n{'='*96}")
    print(f"{'company':22} {'brief':18} {'ok':3} {'rows':5} {'secs':5} reviews / reason")
    print("-" * 96)
    for r in records:
        rows = (r.get("query") or {}).get("row_count", 0)
        revs = ", ".join(f"{v['stage']}{'✓' if v['passed'] else '✗'}" for v in r["reviews"]) or "-"
        detail = revs if r["ok"] else r["reason"]
        print(f"{r['company'][:22]:22} {r['brief'][:18]:18} {'✓' if r['ok'] else '✗':3} "
              f"{rows:<5} {r.get('secs', 0):<5.0f} {detail[:44]}")
    ok = sum(1 for r in records if r["ok"])
    print("-" * 96)
    print(f"{ok}/{len(records)} onboarded")
    print(f"READ THIS FIRST -> {outdir}/index.md   (per-company reports + how to test each query)")

    if a.aggregate and records:
        _aggregate(records, outdir)


if __name__ == "__main__":
    main()
