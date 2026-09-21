"""LIVE A/B of the two query authors -- text vs index -- with a real LLM.

Gated on ``ANTHROPIC_API_KEY``. With no key it prints a notice and exits 0 (so CI / a keyless
run is not blocked); the deterministic ceiling in ``query_engine_eval.py`` stands on its own.

With a key it serves each chosen scenario's pages on a local HTTP server and runs the pipeline's
own ``write_query`` twice -- ``author_engine="text"`` and ``author_engine="index"`` -- against
the SAME fetched page, then compares row count + completeness to the scenario's known-good
``expected`` rows. This measures the realized ACCURACY gap (does the model actually find the
query), on top of the deterministic CEILING (could any policy).

Run:  env/bin/python evals/llm_compare.py            # a default subset
      ANTHROPIC_API_KEY=... env/bin/python evals/llm_compare.py
"""

from __future__ import annotations

import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO / "tests"))

from messy_html import all_scenarios  # noqa: E402

# a small, representative subset spanning the shapes the index engine is meant to help with.
_SUBSET = {
    "spa_false_flag",          # flat text fields, single region -- index engine's sweet spot
    "duplicate_featured",      # needs section scoping the index engine can't express
    "regex_products",          # nested + regex + attr -- text-engine only
    "flat_sibling_press",      # href + sibling date -- text-engine only
    "table_with_section_headers",
    "rss_feed",                # xml -- both should manage
}


def _make_server(pages: dict, content_types: dict) -> "tuple[HTTPServer, str]":
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):  # quiet
            pass

        def do_GET(self):
            path = self.path.split("?")[0]
            if path not in pages:
                self.send_response(404); self.end_headers(); return
            body = pages[path].encode()
            self.send_response(200)
            self.send_header("Content-Type", content_types.get(path, "text/html; charset=utf-8"))
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_port}"


def _run_live() -> None:
    from webclient import WebClient
    from webclient.clients.llm import LlmClient
    from webclient.pipelines.onboarding import Brief, write_query

    key = os.environ["ANTHROPIC_API_KEY"]
    scenarios = [s for s in all_scenarios() if s.name in _SUBSET]
    print(f"Running LIVE text-vs-index A/B on {len(scenarios)} scenario(s)\n")
    rows = []
    for sc in scenarios:
        srv, base = _make_server(sc.pages, sc.content_types)
        entry = base + sc.entry
        want = 0 if sc.expected == "EMPTY" else len(sc.expected)
        brief = Brief(description=sc.desc, fields=list(sc.fields), hints=sc.notes)
        out = {}
        try:
            with WebClient() as wc:
                for engine in ("text", "index"):
                    client = LlmClient(auth=key)  # fresh client (own conversation) per engine
                    art = write_query(entry, brief, wc=wc, llm=client, browser="never",
                                      author_engine=engine, retries=3)
                    out[engine] = (art.row_count if art else 0,
                                   bool(art and art.complete), f"${getattr(client,'spent',0):.4f}")
        finally:
            srv.shutdown()
        rows.append((sc.name, want, out))
        print(f"{sc.name:28} want={want:>2}  "
              f"text={out.get('text')}  index={out.get('index')}")
    # tally
    print("\n=== A/B tally (rows == want AND complete) ===")
    for label, key_ in (("text", "text"), ("index", "index")):
        ok = sum(1 for _n, w, o in rows if o.get(key_, (0, False))[0] == w and (w == 0 or o[key_][1]))
        print(f"  {label:6}: {ok}/{len(rows)}")


def main() -> None:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("LLM A/B comparison: NEEDS API KEY (ANTHROPIC_API_KEY not set) -- skipped.")
        print("The recommendation rests on the deterministic ceiling in query_engine_eval.py.")
        print("With a key, this harness runs write_query(author_engine='text') vs "
              "('index') on a\nlocal server per scenario and compares row_count/complete to "
              "the known-good expected rows.")
        return
    _run_live()


if __name__ == "__main__":
    main()
