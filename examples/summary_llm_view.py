"""Case study: card() + facets as an LLM's token-lean view of a page.

``doc.card()`` returns a lean :class:`PageCard` -- url / kind / title / description /
the flags that fired / how the bytes were obtained -- what an LLM reads *instead of*
raw HTML. For a markup page the separate ``metadata()`` (head/schema) and
``structure()`` (body shape: keys and counts, not the whole document) facets round it
out. This prints that view for three very different pages so you can see how compact
and uniform it is.

Sites: text.npr.org (an article), books.toscrape.com (a shop), httpbin.org/json
(a JSON API) -- all scraper-friendly.

Features: card()/PageCard, metadata(), structure(), the facet models.
Run:  env/bin/python examples/summary_llm_view.py
"""

from __future__ import annotations

import json

from webclient import WebClient, WebException

UA = "webclient-examples/0.1"
PAGES = [
    ("news article", "https://text.npr.org/"),
    ("shop listing", "https://books.toscrape.com/"),
    ("JSON API", "https://httpbin.org/json"),
]


def show(wc: WebClient, label: str, url: str) -> None:
    print(f"\n=== {label}: {url} ===")
    doc = wc.fetch(url)
    # the lean overview -- works on any kind (a JSON API included).
    view = {"card": doc.card().model_dump(exclude_none=True)}
    # the head/schema + body-shape facets apply to markup pages only.
    if doc.kind in ("html", "xml"):
        st = doc.structure().model_dump(exclude_none=True)
        st["toc"] = st.get("toc", [])[:3]           # trim long lists so the print stays readable
        st["link_sample"] = st.get("link_sample", [])[:3]
        view["metadata"] = doc.metadata().model_dump(exclude_none=True)
        view["structure"] = st
    print(json.dumps(view, indent=2, default=str)[:1400])


def main() -> None:
    with WebClient(default_headers={"User-Agent": UA}, min_interval=0.3, timeout=25) as wc:
        for label, url in PAGES:
            try:
                show(wc, label, url)
            except WebException as exc:
                print(f"  {label}: {exc.error.type} (skipped)")


if __name__ == "__main__":
    main()
