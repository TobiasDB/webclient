"""demo_onboarding.py -- the company-onboarding pipeline, end to end, offline.

Given a BRIEF (a dataset description) and a company, the pipeline searches for
seeds, crawls toward the dataset (the model steering the frontier), picks + evaluates
candidate sources, then writes the Reference, the Resolve policy, and a lazy web
query for the best one. Here the model and web search are SCRIPTED stubs and the
"company" is a local site, so it runs with no network and no API key:

    env/bin/python demo_onboarding.py

In production you inject a real ``llm(prompt)->str`` (a Claude call) and a real
``search`` (e.g. ``pipelines.ddg_search``).
"""

from __future__ import annotations

import http.server
import json
import threading
from typing import Any

from webclient import WebClient, from_blob, wq
from webclient.pipelines import (
    Brief,
    SearchHit,
    crawl_from_seeds,
    evaluate_candidates,
    search_web,
    select_candidates,
    write_query,
    write_reference,
    write_resolve,
)

HOME = b"""
<html><head><title>Acme Robotics</title></head><body>
  <nav><a href="/products">Products</a><a href="/about">About</a><a href="/careers">Careers</a></nav>
  <main><h1>Acme Robotics</h1><p>We build widgets and sprockets.</p></main>
  <footer><a href="/privacy">Privacy</a><a href="https://twitter.com/acme">Follow us</a></footer>
</body></html>
"""
PRODUCTS = b"""
<html><head><title>Products - Acme</title></head><body><main>
  <h1>Our products</h1>
  <div class="product"><span class="name">Widget</span><span class="price">$10</span></div>
  <div class="product"><span class="name">Sprocket</span><span class="price">$20</span></div>
  <div class="product"><span class="name">Cog</span><span class="price">$30</span></div>
</main></body></html>
"""


class Handler(http.server.BaseHTTPRequestHandler):
    ROUTES = {"/": HOME, "/products": PRODUCTS,
              "/about": b"<html><body><h1>About</h1></body></html>",
              "/careers": b"<html><body><h1>Careers</h1></body></html>"}

    def do_GET(self):  # noqa: N802
        body = self.ROUTES.get(self.path, b"not found")
        self.send_response(200 if self.path in self.ROUTES else 404)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002  quiet
        pass


def serve() -> str:
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{server.server_port}"


def make_stubs(base: str, blob: str):
    """A scripted search + model for the offline demo (prod injects the real ones)."""
    def search(query: str, k: int) -> list[SearchHit]:
        return [SearchHit(url=f"{base}/", title="Acme Robotics", snippet="widgets")]

    def llm(prompt: str) -> str:
        if "web-search query" in prompt:
            return "Acme Robotics products"
        if "frontier links" in prompt:
            for line in prompt.splitlines():
                s = line.strip()
                if s[:1].isdigit() and "/products" in s:
                    return f"[{s.split('.', 1)[0]}]"
            return "[]"
        if "crawled pages" in prompt:
            return json.dumps([{"url": f"{base}/products", "kind": "page",
                                "tier": "must", "note": "the product listing"}])
        if "Assess this page" in prompt:
            return json.dumps({"dataset_present": True, "is_queryable": True,
                               "completeness": "full", "has_pagination": False,
                               "scrapability": 9, "verdict": "a full product list"})
        if "query DSL" in prompt or "portable blob" in prompt:
            return f"```json\n{blob}\n```"
        return "{}"

    return search, llm


def out(label: str, value: Any) -> None:
    print(f"  {label:12} {value}")


def main() -> None:
    base = serve()
    brief = Brief(description="the company's products", fields=["name", "price"])
    # the query the scripted model will "author" (prod: the LLM writes it from the skeleton)
    blob = (
        wq.ref.resolve().select_all(".product")
        .extract(name=wq.doc.select(".name").text_content,
                 price=wq.doc.select(".price").text_content)
        .project().to_blob()
    )
    search, llm = make_stubs(base, blob)

    with WebClient() as wc:
        print("BRIEF:", brief.description, "| fields:", brief.fields, "\n")

        print("1. search_web -> seeds")
        seeds = search_web(brief, "Acme Robotics", search=search, llm=llm)
        out("seeds", [s.url.replace(base, "") or "/" for s in seeds])

        print("2. crawl_from_seeds (model steers the frontier)")
        crawl = crawl_from_seeds(seeds, brief, wc=wc, llm=llm, browser=False, max_pages=8)
        out("crawled", [(p.final_url or p.url).replace(base, "") for p in crawl.pages])

        print("3. select_candidates")
        candidates = select_candidates(crawl, brief, llm=llm)
        out("candidates", [(c.tier, c.url.replace(base, "")) for c in candidates])

        print("4. evaluate_candidates")
        ev = evaluate_candidates(candidates, brief, wc=wc, llm=llm, browser="never")
        out("best", ev and {"url": ev.url.replace(base, ""), "queryable": ev.is_queryable,
                            "scrapability": ev.scrapability, "verdict": ev.verdict})

        assert ev is not None
        print("5. write_reference (deterministic from the candidate)")
        ref = write_reference(ev, wc=wc)
        out("reference", ref.url)

        print("6. write_resolve (deterministic from the page's signals)")
        page = wc.fetch(ev.url)
        resolve = write_resolve(page.signals())
        out("resolve", {"browser": resolve.browser, "proxy": resolve.proxy})

        print("7. write_query (model authors it from the skeleton)")
        query = write_query(ev.url, brief, wc=wc, llm=llm, browser="never")
        assert query is not None
        out("query", query.describe)

        print("\nRUN the authored query against the source:")
        rows = from_blob(query.blob).collect(wc.ref(ev.url))
        for r in rows:
            out("row", r)


if __name__ == "__main__":
    main()
