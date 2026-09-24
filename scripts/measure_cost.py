"""Measure what the website's /cost page shows (story F1): the size of each view the client
can hand a model -- raw HTML, markdown, skeleton (collapsed), card -- for a few of the
site's own pages, and what authoring one query cost in the recorded onboarding run
(model calls, prompt / response characters). Tokens are ESTIMATED at 4 chars per token;
the page says so. Nothing here is typed by hand: re-run it and the page changes.

    python scripts/measure_cost.py [--site http://127.0.0.1:4321] [--out path/to/cost.json]
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date
from pathlib import Path

from webclient import WebClient

PAGES = [("/", "Home (product grid)", False), ("/changelog", "Changelog (SPA, rendered)", True),
         ("/case-studies", "Case studies p1", False), ("/benchmarks", "Benchmarks (6000 rows)", False)]


def measure_pages(site: str) -> list[dict]:
    out = []
    with WebClient(timeout=30.0) as wc:
        for path, label, browser in PAGES:
            doc = wc.fetch(site + path, browser="auto" if browser else False)
            card = json.dumps(doc.card().model_dump(mode="json"))
            out.append({"page": path, "label": label, "tier": doc.transport().final_tier,
                        "html": len(doc.content or b""), "markdown": len(doc.markdown()),
                        "skeleton": len(doc.skeleton(collapse=True)), "card": len(card)})
            if browser:
                wc.release(doc)
    return out


def measure_authoring(site: str) -> dict:
    """Run the scripted onboarding (the demo model) against the site's home grid and count
    the model traffic it took to author the query."""
    from webclient.pipelines import Brief, SearchHit, onboard_company

    shop = site + "/"
    code = ('wq.doc.select_all("div.card").extract(title=wq.doc.select(".title").attr("text"), '
            'price=wq.doc.select(".price").attr("text")).project()')
    calls = {"n": 0, "prompt_chars": 0, "response_chars": 0}

    def llm(prompt: str) -> str:
        if "frontier links" in prompt:
            answer = "[]"
        elif "crawled pages" in prompt:
            answer = json.dumps([{"url": shop, "kind": "page", "tier": "must", "note": "the product grid"}])
        elif "Assess this page" in prompt:
            answer = json.dumps({"dataset_present": True, "is_queryable": True, "completeness": "full",
                                 "has_pagination": False, "scrapability": 9, "verdict": "a product list"})
        elif "query code" in prompt or "write a query" in prompt:
            answer = f"here is the query:\n{code}"
        else:
            answer = "{}"
        calls["n"] += 1
        calls["prompt_chars"] += len(prompt)
        calls["response_chars"] += len(answer)
        return answer

    t0 = time.time()
    with WebClient(timeout=30.0) as wc:
        result = onboard_company("Roasters", Brief(description="the featured products with their prices",
                                                   fields=["title", "price"], search="products"),
                                 wc=wc, llm=llm,
                                 search=lambda q, k: [SearchHit(url=shop, title="Roasters", snippet="products")],
                                 browser=False)
    return {"ok": result.ok, "calls": calls["n"], "prompt_chars": calls["prompt_chars"],
            "response_chars": calls["response_chars"], "seconds": round(time.time() - t0, 2)}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--site", default="http://127.0.0.1:4321")
    ap.add_argument("--out", default=None)
    args = ap.parse_args(argv)
    data = {"measured": date.today().isoformat(), "site": args.site, "model": "stub (the scripted demo model)",
            "chars_per_token": 4, "pages": measure_pages(args.site), "authoring": measure_authoring(args.site)}
    text = json.dumps(data, indent=1)
    if args.out:
        Path(args.out).write_text(text + "\n")
        print(f"wrote {args.out}")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
