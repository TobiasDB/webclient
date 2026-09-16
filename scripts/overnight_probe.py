#!/usr/bin/env python
"""Overnight onboarding probe -- fired by a 10-minute cron to exercise the pipeline all night.

Each run picks ONE untested (brief, live-site) target, ALTERNATING a NEW brief (the ones
saved under webclient/pipelines/briefs/: job-postings, github-releases, changelog,
pricing-plans, podcast-episodes, docs-pages) with an existing/CURRENT one (product-catalogue,
quotes, countries, blog, ir-news) to keep a clean ~1:1 new/current ratio. It runs the full
onboarding pipeline on that site via the `claude -p` shim (cheapest model, budget-tracked, no
API key), writes a per-run .md/.json/.dataset.json, and appends to a ledger so the next run
picks a fresh target and the morning review sees everything in one place.

    env/bin/python scripts/overnight_probe.py            # one probe (the cron runs this)
    env/bin/python scripts/overnight_probe.py --status   # print the ledger summary, run nothing
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

OUT = ROOT / "harness_runs" / "overnight"
LEDGER = OUT / "ledger.json"

NEW_BRIEFS = ["job-postings", "github-releases", "changelog",
              "pricing-plans", "podcast-episodes", "docs-pages"]
CURRENT_BRIEFS = ["product-catalogue", "quotes", "countries", "blog", "ir-news"]

# brief key -> candidate untested live seeds (label, url). Deliberately diverse shapes:
# RSS/Atom feeds (exercise XML), JSON APIs, real tables, SPA blogs, anti-bot IR pages.
POOL: dict[str, list[tuple[str, str]]] = {
    "github-releases": [
        ("react", "https://github.com/facebook/react/releases"),
        ("cpython", "https://github.com/python/cpython/releases"),
        ("node", "https://github.com/nodejs/node/releases"),
        ("kubernetes", "https://github.com/kubernetes/kubernetes/releases"),
        ("rust", "https://github.com/rust-lang/rust/releases"),
        ("vue-core", "https://github.com/vuejs/core/releases"),
    ],
    "changelog": [
        ("github-changelog", "https://github.blog/changelog/"),
        ("raycast", "https://www.raycast.com/changelog"),
        ("linear", "https://linear.app/changelog"),
        ("vercel", "https://vercel.com/changelog"),
        ("warp", "https://www.warp.dev/changelog"),
    ],
    "pricing-plans": [
        ("vercel", "https://vercel.com/pricing"),
        ("render", "https://render.com/pricing"),
        ("supabase", "https://supabase.com/pricing"),
        ("digitalocean", "https://www.digitalocean.com/pricing"),
        ("fly", "https://fly.io/pricing"),
    ],
    "podcast-episodes": [
        ("changelog", "https://changelog.com/podcast"),
        ("talkpython", "https://talkpython.fm/episodes/all"),
        ("realpython", "https://realpython.com/podcasts/rpp/"),
        ("syntax", "https://syntax.fm/"),
        ("shoptalk", "https://shoptalkshow.com/episodes/"),
    ],
    "docs-pages": [
        ("python", "https://docs.python.org/3/"),
        ("django", "https://docs.djangoproject.com/en/stable/"),
        ("astro", "https://docs.astro.build/en/getting-started/"),
        ("redis", "https://redis.io/docs/latest/"),
        ("fastapi", "https://fastapi.tiangolo.com/"),
    ],
    "job-postings": [
        ("weworkremotely", "https://weworkremotely.com/categories/remote-programming-jobs"),
        ("gitlab", "https://about.gitlab.com/jobs/all-jobs/"),
        ("remoteok", "https://remoteok.com/"),
        ("basecamp", "https://37signals.com/careers/"),
    ],
    "product-catalogue": [
        ("webscraper-phones", "https://webscraper.io/test-sites/e-commerce/static/phones/touch"),
        ("oxylabs-sandbox", "https://sandbox.oxylabs.io/products"),
        ("books-mystery", "https://books.toscrape.com/catalogue/category/books/mystery_3/index.html"),
        ("scrapingcourse", "https://www.scrapingcourse.com/ecommerce/"),
    ],
    "quotes": [
        ("quotes-love", "https://quotes.toscrape.com/tag/love/"),
        ("quotes-page2", "https://quotes.toscrape.com/page/2/"),
        ("quotes-inspirational", "https://quotes.toscrape.com/tag/inspirational/"),
    ],
    "countries": [
        ("restcountries-europe", "https://restcountries.com/v3.1/region/europe"),
        ("scrapethissite-index", "https://www.scrapethissite.com/pages/"),
        ("wikipedia-population", "https://en.wikipedia.org/wiki/List_of_countries_by_population_(United_Nations)"),
    ],
    "blog": [
        ("python-insider", "https://blog.python.org/"),
        ("rust-blog", "https://blog.rust-lang.org/"),
        ("aws-news", "https://aws.amazon.com/blogs/aws/"),
        ("gitlab-blog", "https://about.gitlab.com/blog/"),
        ("netflix-tech", "https://netflixtechblog.com/"),
    ],
    "ir-news": [
        ("apple-newsroom", "https://www.apple.com/newsroom/rss-feed.rss"),
        ("microsoft-blogs", "https://blogs.microsoft.com/feed/"),
        ("intel-rss", "https://newsroom.intel.com/feed"),
        ("tesla-press", "https://ir.tesla.com/press"),
        ("zoom-ir", "https://investors.zoom.us/news-releases"),
    ],
}

# sites already exercised by the round-2 live run -- pre-seed as done so we don't repeat them.
_ALREADY = [
    "https://quotes.toscrape.com/scroll",
    "https://www.scrapethissite.com/pages/ajax-javascript/",
    "https://www.scrapethissite.com/pages/forms/",
    "https://webscraper.io/test-sites/e-commerce/static/computers/laptops",
    "https://vercel.com/blog", "https://kubernetes.io/blog/",
    "https://nvidianews.nvidia.com/", "https://ir.amd.com/news-events/press-releases",
]


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:60]


def _ledger() -> dict:
    if LEDGER.exists():
        try:
            return json.loads(LEDGER.read_text())
        except ValueError:
            pass
    return {"done": list(_ALREADY), "results": [], "counts": {"new": 0, "current": 0}}


def _save_ledger(led: dict) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    LEDGER.write_text(json.dumps(led, indent=2))


def _pick(led: dict) -> "tuple[str, str, str, str] | None":
    """(category, brief_key, label, url) for the next untested target, alternating new/current
    to keep the ratio clean; falls back to the other category if one is exhausted."""
    done = set(led["done"])
    counts = led["counts"]
    order = (["new", "current"] if counts["new"] <= counts["current"]
             else ["current", "new"])
    for category in order:
        briefs = NEW_BRIEFS if category == "new" else CURRENT_BRIEFS
        # rotate brief start by how many we've done in this category, for spread
        start = counts[category] % len(briefs)
        for i in range(len(briefs)):
            key = briefs[(start + i) % len(briefs)]
            for label, url in POOL.get(key, []):
                if url not in done:
                    return category, key, label, url
    return None


def _load_brief(key: str):
    from webclient.pipelines.__main__ import _load_brief as load

    return load(key)


def _company_of(key: str, label: str) -> str:
    return f"{label} ({key})"


def _run(category: str, key: str, label: str, url: str) -> dict:
    from claude_llm_adapter import claude_shim_client
    from webclient import WebClient
    from webclient.pipelines import Budget, SearchHit, onboard

    brief = _load_brief(key)
    company = _company_of(key, label)

    def search(query: str, k: int = 6) -> "list[SearchHit]":
        return [SearchHit(url=url, title=company, snippet=f"{company} (overnight seed)")]

    budget = Budget()
    llm = claude_shim_client(budget=budget)
    started = dt.datetime.now()
    with WebClient() as wc:
        results = onboard([company], brief, wc=wc, llm=llm, search=search,
                          max_pages=int(brief.crawl.get("max_pages", 10)),
                          browser=True, budget=budget, review=False)
    r = results[0]
    elapsed = (dt.datetime.now() - started).total_seconds()
    rec = {
        "category": category, "brief": key, "label": label, "url": url,
        "company": company, "ok": r.ok, "reason": r.reason,
        "source": (r.evaluation.url if r.evaluation else None),
        "rows": (r.query.row_count if r.query else 0),
        "query": (r.query.describe if r.query else None),
        "blob": (r.query.blob if r.query else None),
        "sample": (list(r.query.sample)[:5] if r.query else []),
        "cost_usd": round(budget.spent_usd, 4), "calls": budget.calls,
        "elapsed_s": round(elapsed, 1),
        "ts": started.strftime("%Y-%m-%d %H:%M:%S"),
    }
    return rec


def _write_run(rec: dict) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    stem = f"{rec['ts'][:10].replace('-', '')}-{rec['ts'][11:].replace(':', '')}-{_slug(rec['brief'] + '-' + rec['label'])}"
    (OUT / f"{stem}.json").write_text(json.dumps(rec, indent=2, default=str))
    status = "✅" if rec["ok"] else "❌"
    md = [
        f"# {status} {rec['company']} × {rec['brief']}  ({rec['category']})",
        "",
        f"- **URL:** {rec['url']}",
        f"- **Source chosen:** {rec['source']}",
        f"- **Result:** {'onboarded' if rec['ok'] else 'not onboarded — ' + rec['reason']}",
        f"- **Rows:** {rec['rows']}  ·  **Cost:** ${rec['cost_usd']}  ·  **Calls:** {rec['calls']}  ·  **{rec['elapsed_s']}s**",
        "",
        "## Query", "```", (rec["query"] or "(none)"), "```", "",
        "## Sample rows", "```json",
        json.dumps(rec["sample"], indent=2, default=str)[:1500], "```",
    ]
    (OUT / f"{stem}.md").write_text("\n".join(md))
    if rec["blob"]:
        (OUT / f"{stem}.blob.json").write_text(rec["blob"])


def _append_summary(led: dict) -> None:
    rows = led["results"]
    ok = sum(1 for r in rows if r["ok"])
    lines = [
        "# Overnight onboarding probes",
        "",
        f"{ok}/{len(rows)} onboarded · new={led['counts']['new']} current={led['counts']['current']}",
        "",
        "| when | status | brief | site | rows | cost | reason |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in rows[-60:]:
        st = "✅" if r["ok"] else "❌"
        lines.append(f"| {r['ts'][5:]} | {st} | {r['brief']} | {r['label']} | "
                     f"{r['rows']} | ${r['cost_usd']} | {r['reason'] or '-'} |")
    (OUT / "summary.md").write_text("\n".join(lines))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--status", action="store_true", help="print the ledger summary, run nothing")
    a = ap.parse_args()

    led = _ledger()
    if a.status:
        ok = sum(1 for r in led["results"] if r["ok"])
        print(f"overnight: {ok}/{len(led['results'])} onboarded · "
              f"new={led['counts']['new']} current={led['counts']['current']} · "
              f"{len(led['done'])} sites tested")
        return 0

    pick = _pick(led)
    if pick is None:
        print("overnight: pool exhausted — every candidate site has been tested. Nothing to do.")
        return 0
    category, key, label, url = pick
    print(f"overnight: probing [{category}] {key} × {label}  ({url})")
    try:
        rec = _run(category, key, label, url)
    except Exception as exc:  # noqa: BLE001 - never let one bad site break the loop
        rec = {
            "category": category, "brief": key, "label": label, "url": url,
            "company": _company_of(key, label), "ok": False,
            "reason": f"probe crashed: {type(exc).__name__}: {exc}",
            "source": None, "rows": 0, "query": None, "blob": None, "sample": [],
            "cost_usd": 0.0, "calls": 0, "elapsed_s": 0.0,
            "ts": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        traceback.print_exc()

    # record + advance the ledger even on failure, so we never re-probe the same site
    led["done"].append(url)
    led["results"].append(rec)
    led["counts"][category] += 1
    _save_ledger(led)
    _write_run(rec)
    _append_summary(led)
    print(f"overnight: {'OK' if rec['ok'] else 'FAIL'} — {rec['rows']} row(s), "
          f"${rec['cost_usd']}, {rec['elapsed_s']}s — {rec['reason'] or 'onboarded'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
