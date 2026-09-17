#!/usr/bin/env python
"""LLM eval: can the model AUTHOR the hard queries the messy-HTML scenarios demand?

For each :class:`~tests.messy_html.Scenario` we serve its pages locally, run the real
``write_query`` stage (skeleton -> model -> parse -> test), then execute the model's authored
query and compare rows to ``expected``. Unlike ``tests/test_messy_html.py`` (which runs the
KNOWN-GOOD solution to prove each shape is solvable), this measures whether the MODEL finds an
equivalent query on its own -- nested resolves, sibling-no-root, regex splits, empty-vs-
archived events, an SPA false flag, an RSS/XML feed and a JSON API.

LLM selection mirrors the harness: ANTHROPIC_API_KEY (lightweight) else local ``claude -p``.

    env/bin/python scripts/query_eval.py                 # all scenarios
    env/bin/python scripts/query_eval.py --only rss_feed json_api
    ANTHROPIC_API_KEY=... env/bin/python scripts/query_eval.py
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "scripts"))

from messy_html import all_scenarios  # noqa: E402

from webclient import WebClient  # noqa: E402
log = logging.getLogger("query_eval")

from webclient.pipelines.onboarding import (  # noqa: E402
    Brief,
    run_query,
    write_query,
)


def _serve(pages: dict[str, str], ctypes: dict[str, str]) -> ThreadingHTTPServer:
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):  # noqa: ANN002, ANN201 - silence access log
            pass

        def do_GET(self):  # noqa: N802
            path = self.path.split("?")[0]
            body = pages.get(path)
            if body is None:
                self.send_response(404)
                self.end_headers()
                return
            raw = body.encode()
            self.send_response(200)
            self.send_header("Content-Type", ctypes.get(path, "text/html; charset=utf-8"))
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def _normalize(expected, base: str) -> list:
    def fix(v):
        if isinstance(v, str):
            return v.replace("https://SITE", base)
        if isinstance(v, dict):
            return {k: fix(x) for k, x in v.items()}
        return v
    return [fix(r) for r in expected]


def _rows(result) -> list:
    return [r.model_dump() if hasattr(r, "model_dump") else r for r in result]


def _make_llm(shim: bool, model: str | None):
    # --shim routes the Messages API through `claude -p` in process (budget/pricing/retries,
    # no API key, no server) at the cheapest model; else the Anthropic API if a key is set;
    # else the raw local claude -p adapter. Every path defaults to the cheapest model.
    if shim:
        from claude_llm_adapter import claude_shim_client

        client = claude_shim_client(model=model)
        log.info(f"LLM: claude -p via in-process Messages shim (priced as {client.model}, "
              f"cheapest CLI model)")
        return client
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_BASE_URL"):
        from webclient.pipelines import Budget, LlmClient, cheapest_model

        client = LlmClient(budget=Budget(), model=model or cheapest_model())
        log.info(f"LLM: Anthropic API ({client.model})")
        return client
    from claude_llm_adapter import CHEAPEST_CLI_MODEL, claude_code_llm

    log.info(f"LLM: local Claude Code (claude -p, {CHEAPEST_CLI_MODEL}) — heavy; "
          "run few scenarios at a time (use --shim for budget-tracked calls)")
    return lambda prompt: claude_code_llm(prompt, model=CHEAPEST_CLI_MODEL)


def _brief_of(sc) -> Brief:
    return Brief(name=sc.name, title=sc.name, description=sc.desc, fields=list(sc.fields))


def run_scenario(sc, wc: WebClient, llm) -> dict:
    srv = _serve(sc.pages, sc.content_types)
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    entry = base + sc.entry
    try:
        art = write_query(entry, _brief_of(sc), wc=wc, llm=llm, browser="auto")
        if art is None:
            return {"name": sc.name, "ok": False, "why": "model authored no usable query"}
        rows = _rows(run_query(art, wc=wc))
        if sc.expected == "EMPTY":
            ok = len(rows) == 0
            exp = []
        else:
            exp = _normalize(sc.expected, base)
            ok = rows == exp
        return {
            "name": sc.name, "ok": ok, "complete": art.complete, "rows": rows,
            "expected": exp, "query": art.describe,
        }
    except Exception as exc:  # noqa: BLE001 - a scenario failure shouldn't stop the eval
        return {"name": sc.name, "ok": False, "why": f"{type(exc).__name__}: {exc}"}
    finally:
        srv.shutdown()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--only", nargs="*", default=None, help="run only these scenario names")
    ap.add_argument("--json", type=Path, default=None, help="write full results as JSON here")
    ap.add_argument("--shim", action="store_true",
                    help="route the Messages API through `claude -p` in process (no key/server)")
    ap.add_argument("--model", default=None,
                    help="priced model id (default: the cheapest in the price table)")
    a = ap.parse_args()

    scenarios = [s for s in all_scenarios() if not a.only or s.name in a.only]
    llm = _make_llm(a.shim, a.model)
    results = []
    with WebClient() as wc:
        for sc in scenarios:
            log.info(f"\n=== {sc.name} ===")
            res = run_scenario(sc, wc, llm)
            results.append(res)
            if res["ok"]:
                log.info(f"  PASS  (model wrote a correct query; complete={res.get('complete')})")
            else:
                log.info(f"  FAIL  {res.get('why', '')}")
                if "rows" in res:
                    log.info("    query: %s", (res.get("query") or "").replace("\n", " ")[:200])
                    log.info("    got : %s", json.dumps(res["rows"], default=str)[:400])
                    log.info("    exp : %s", json.dumps(res["expected"], default=str)[:400])

    n_ok = sum(1 for r in results if r["ok"])
    log.info(f"\n{n_ok}/{len(results)} scenarios: model authored a correct query")
    if a.json:
        a.json.write_text(json.dumps(results, indent=2, default=str))
        log.info(f"wrote {a.json}")
    return 0 if n_ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
