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
import io
import json
import logging
import re
import threading
import time
from importlib.resources import files
from pathlib import Path

from webclient import WebClient
from webclient.core.reference.models import BrowserConfig
from webclient.pipelines import Brief, SearchHit, onboard_company

# per-thread log capture: every company runs in its own thread against ONE shared client;
# this router sends that thread's pipeline log lines to its own buffer (-> its .log file),
# so parallel runs stay separable without separate processes.
_local = threading.local()


class _ThreadLogRouter(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        buf = getattr(_local, "buf", None)
        if buf is not None:
            try:
                buf.write(self.format(record) + "\n")
            except Exception:
                pass


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
    "blog": Brief(
        name="blog", title="Blog posts",
        description="every blog / news post listed on the page, each with its title, "
                    "publication date, author and a link to the full post",
        fields=["title", "date", "url", "author?", "summary?"],
    ),
    "ir-news": _packaged_brief("ir-news"),
}

# (brief key, company, [seed urls]) -- curated so no live search runs. A LARGE, mixed set
# spread across many distinct domains: rotate a random --sample of it each run so we don't
# hammer any one site (and don't get blocked). The sandbox sites are reliable static
# targets; the IR pages are the hard, real, mostly client-rendered / anti-bot cases.
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
    ("ir-news", "Cloudflare", ["https://cloudflare.net/news/default.aspx"]),
    ("ir-news", "MongoDB", ["https://investors.mongodb.com/news-releases"]),
    ("ir-news", "Atlassian", ["https://investors.atlassian.com/news-and-events/news"]),
    ("ir-news", "Twilio", ["https://investors.twilio.com/news/default.aspx"]),
    ("ir-news", "Okta", ["https://investor.okta.com/news-releases"]),
    ("ir-news", "CrowdStrike", ["https://ir.crowdstrike.com/news-releases"]),
    ("ir-news", "Zscaler", ["https://ir.zscaler.com/news-releases"]),
    ("ir-news", "HubSpot", ["https://ir.hubspot.com/news"]),
    ("ir-news", "Elastic", ["https://ir.elastic.co/news/default.aspx"]),
    ("ir-news", "Confluent", ["https://investors.confluent.io/news/default.aspx"]),
    ("ir-news", "Cisco", ["https://newsroom.cisco.com/c/r/newsroom/en/us/index.html"]),
    ("ir-news", "Oracle", ["https://www.oracle.com/news/"]),
    # seed URLs found via web search (real, current) -- the seed is a homepage/blog/newsroom,
    # not necessarily the dataset page; the crawl finds the dataset from there.
    ("blog", "Stripe", ["https://stripe.com/blog"]),
    ("blog", "Cloudflare", ["https://blog.cloudflare.com/"]),
    ("ir-news", "Microsoft", ["https://news.microsoft.com/source/tag/press-releases/"]),
    ("product-catalogue", "Framework", ["https://frame.work/marketplace/parts"]),
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
        f"- **Rows extracted (sample tested):** {q['row_count'] if q else 0}",
        (f"- **Full dataset:** {rec['dataset_rows']} rows → [`{slug}.dataset.json`](./{slug}.dataset.json)"
         if rec.get("dataset_rows") is not None else "- **Full dataset:** —"),
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


# ---------------------------------------------------------------------------- #
# parent: run cases as concurrent SESSIONS on ONE shared client, collect, summarise
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


def _run_case(wc: WebClient, llm, case: tuple[str, str, list[str]], outdir: Path,
              *, browser: bool, review: bool) -> dict:
    """Onboard ONE company against the SHARED client (a session's fetches lease pages from
    the one browser pool). Its pipeline log is captured to this thread's buffer -> .log,
    and the structured result + a readable report are written."""
    brief_key, company, urls = case
    slug = _slug(brief_key, company)
    prefix = outdir / slug
    buf = io.StringIO()
    _local.buf = buf  # route THIS thread's pipeline log lines to buf
    t0 = time.monotonic()
    try:
        # a per-company SESSION isolates cookies/headers/state (shares the one browser pool),
        # so concurrent companies don't cross-contaminate each other.
        sess = wc.session()
        r = onboard_company(company, BRIEFS[brief_key], wc=sess, llm=llm,
                            search=_canned_search(urls, company), browser=browser, review=review)
        rec = _result_dict(brief_key, r)
        # WRITE THE DATASET: run the authored query for the FULL set of rows (not just the
        # 5-row sample) and save it, so a passing case yields the actual extracted dataset.
        if r.ok and r.query is not None:
            from webclient.pipelines import run_query
            try:
                rows = run_query(r.query, wc=sess)
                prefix.with_suffix(".dataset.json").write_text(json.dumps(rows, indent=2, default=str))
                rec["dataset_rows"] = len(rows)
            except Exception as exc:  # noqa: BLE001 - dataset run failure shouldn't sink the case
                buf.write(f"\ndataset run failed: {type(exc).__name__}: {exc}\n")
                rec["dataset_rows"] = None
    except Exception as exc:  # a bad case must not sink the rest
        buf.write(f"\nEXCEPTION: {type(exc).__name__}: {exc}\n")
        rec = {"company": company, "brief": brief_key, "ok": False,
               "reason": f"crashed: {type(exc).__name__}: {exc}", "source": None,
               "query": None, "reviews": [], "trace": []}
    finally:
        _local.buf = None
    rec["secs"] = round(time.monotonic() - t0, 1)
    rec["slug"] = slug
    prefix.with_suffix(".log").write_text(buf.getvalue())
    prefix.with_suffix(".json").write_text(json.dumps(rec, indent=2, default=str))
    blob_path = prefix.with_suffix(".blob.json")
    if rec.get("query"):
        blob_path.write_text((rec["query"] or {}).get("blob", ""))
    prefix.with_suffix(".md").write_text(_report_md(rec, slug, str(blob_path)))
    rows = (rec.get("query") or {}).get("row_count", 0)
    ds = rec.get("dataset_rows")
    extra = f", dataset {ds} rows" if ds is not None else ""
    print(f"  {'✓' if rec['ok'] else '✗'} {company} × {brief_key}  ({rows} rows{extra}, {rec['secs']:.0f}s)")
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
    ap.add_argument("--brief", action="append", help="only these brief key(s)")
    ap.add_argument("--only", action="append", help="only these compan(y/ies)")
    ap.add_argument("--parallel", type=int, default=2,
                    help="companies to run concurrently as SESSIONS on ONE shared browser")
    ap.add_argument("--pool", type=int, default=0,
                    help="browser page-pool size (0 = parallel+2); one browser, this many pages")
    # decreased crawl: a small page budget forces the crawl to be efficient (and cheap)
    ap.add_argument("--max-pages", type=int, default=6, help="crawl page budget per company")
    ap.add_argument("--rounds", type=int, default=2, help="LLM frontier-pick rounds per crawl")
    ap.add_argument("--depth", type=int, default=2, help="max crawl link depth")
    # rotation: run a random sample of the (large) case list so we don't hammer one site
    ap.add_argument("--sample", type=int, default=0, help="run a random N-case sample (0 = all)")
    ap.add_argument("--seed", type=int, default=0, help="random seed for --sample (0 = time-based)")
    # browser hardening
    ap.add_argument("--headful", action="store_true", help="headless OFF (needs a display; less bot-detectable)")
    ap.add_argument("--channel", default=None, help="browser channel, e.g. 'chrome' for real Google Chrome")
    ap.add_argument("--max", type=int, default=0, help="stop after N cases (0 = all)")
    ap.add_argument("--resume", metavar="DIR", help="reuse finished cases in DIR; run only the rest")
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--no-review", action="store_true")
    ap.add_argument("--verbose", action="store_true", help="capture logs at DEBUG")
    ap.add_argument("--aggregate", action="store_true", help="add a cross-matrix review at the end")
    ap.add_argument("--budget", type=float, default=0.0, help="cap LLM spend (USD) when using the API key")
    ap.add_argument("--shim", action="store_true",
                    help="route the Messages API through `claude -p` in process (budget-tracked, "
                         "no API key, no server) at the cheapest model")
    ap.add_argument("--model", default=None,
                    help="priced model id (default: the cheapest in the price table)")
    a = ap.parse_args()

    # decreased crawl size: push the small budget into every brief's crawl block
    for b in BRIEFS.values():
        b.crawl.update(max_pages=a.max_pages, rounds=a.rounds, depth=a.depth)

    # Prefer a REAL Anthropic API key: LLM calls become lightweight httpx requests, avoiding
    # the heavy nested `claude -p` processes (each a full Claude Code instance) whose combined
    # footprint trips the background-task supervisor. Fall back to the local claude CLI when
    # no key/base-url is set (keep --parallel low then: the nested procs get the task killed).
    import contextlib
    import os

    _client = None
    if a.shim:
        # route the Messages API through `claude -p` IN PROCESS: budget-tracked + retried
        # like the real API, no key, no server -- and at the cheapest model. One nested CLI
        # per call still, so keep --parallel low.
        from claude_llm_adapter import claude_shim_client
        from webclient.pipelines import Budget
        _client = claude_shim_client(
            model=a.model, budget=Budget(max_usd=a.budget) if a.budget else Budget())
        base_llm = _client
        print(f"LLM: claude -p via in-process Messages shim (priced as {_client.model}, cheapest "
              "CLI model) — budget-tracked; heavy nested processes, keep --parallel low")
    elif os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_BASE_URL"):
        from webclient.pipelines import Budget, LlmClient, cheapest_model
        _client = LlmClient(budget=Budget(max_usd=a.budget) if a.budget else Budget(),
                            model=a.model or cheapest_model())
        base_llm = _client
        print(f"LLM: Anthropic API ({_client.model}) — lightweight, no nested processes")
    else:
        from claude_llm_adapter import CHEAPEST_CLI_MODEL, claude_code_llm
        base_llm = lambda prompt: claude_code_llm(prompt, model=CHEAPEST_CLI_MODEL)  # noqa: E731
        print(f"LLM: local Claude Code (claude -p, {CHEAPEST_CLI_MODEL}) — heavy nested processes; "
              "keep --parallel low (set ANTHROPIC_API_KEY, or --shim for budget tracking)")

    calls = {"n": 0}
    _lock = threading.Lock()

    def llm(prompt: str) -> str:
        with _lock:
            calls["n"] += 1
        return base_llm(prompt)

    cases = [c for c in CASES
             if (not a.brief or c[0] in a.brief) and (not a.only or c[1] in a.only)]
    if a.sample and a.sample < len(cases):  # rotate a random subset so we don't hammer one site
        import random
        random.Random(a.seed or None).shuffle(cases)
        cases = cases[: a.sample]
    if a.max:
        cases = cases[: a.max]
    outdir = Path(a.resume) if a.resume else Path("harness_runs") / dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    outdir.mkdir(parents=True, exist_ok=True)

    # route the pipeline's log lines to the running thread's buffer (per-company .log).
    # Attach to the onboarding logger (where the pipeline logs) AND its parent (other
    # pipeline submodules), both propagate=False: this both captures the logs and makes
    # the pipeline's own _ensure_logging see a handler already present, so it doesn't add
    # a console handler that would leak to stdout.
    router = _ThreadLogRouter()
    router.setFormatter(logging.Formatter("%(message)s"))
    for name in ("webclient.pipelines", "webclient.pipelines.onboarding"):
        lg = logging.getLogger(name)
        lg.handlers[:] = [router]
        lg.setLevel(logging.DEBUG if a.verbose else logging.INFO)
        lg.propagate = False
    for noisy in ("httpx", "httpcore", "urllib3", "playwright", "asyncio", "werkzeug"):
        logging.getLogger(noisy).setLevel(logging.WARNING)  # keep the console + logs clean

    records: list[dict] = []
    todo: list[tuple[str, str, list[str]]] = []
    for c in cases:  # --resume: keep finished cases, only run the missing ones
        done = _load_existing(outdir, c) if a.resume else None
        (records.append(done) if done is not None else todo.append(c))

    # ONE browser, a pool of pages: each company is a session leasing pages from it -- the
    # right way to parallelise (not one browser process per company).
    pool_pages = a.pool or (a.parallel + 2)
    bc = BrowserConfig(pool_pages=pool_pages, pool_http=max(10, a.parallel * 3),
                       headless=not a.headful, channel=a.channel)  # stealth is on by default
    print(f"{len(cases)} case(s): {len(records)} reused, {len(todo)} to run · {a.parallel} concurrent "
          f"sessions on 1 browser ({pool_pages}-page pool, headless={not a.headful}, "
          f"channel={a.channel or 'chromium'}) · crawl≤{a.max_pages}p/{a.rounds}r -> {outdir}")

    with WebClient(browser_config=bc) as wc:
        with cf.ThreadPoolExecutor(max_workers=max(1, a.parallel)) as pool:
            futs = [pool.submit(_run_case, wc, llm, c, outdir,
                                browser=not a.no_browser, review=not a.no_review) for c in todo]
            for f in cf.as_completed(futs):
                records.append(f.result())
    if _client is not None:  # close the API client
        with contextlib.suppress(Exception):
            _client.close()

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
