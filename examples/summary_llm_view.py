"""Case study: summary() as an LLM's token-lean view of a page.

The `summary()` facet object is designed to be what an LLM reads *instead of* raw
HTML: transport facts, head/schema metadata, and body structure -- keys and counts,
not the whole document. This prints that view for three very different pages so you
can see how compact and uniform it is.

Sites: text.npr.org (an article), books.toscrape.com (a shop), httpbin.org/json
(a JSON API) -- all scraper-friendly.

Features: summary(), summary(*facets) selection, the facet models.
Run:  env/bin/python examples/summary_llm_view.py
"""

from __future__ import annotations

import json

from webclient import WebClient, WebException

UA = "webclient-examples/0.1 (+https://github.com/TobiasDB/webclient)"
PAGES = [
    ("news article", "https://text.npr.org/"),
    ("shop listing", "https://books.toscrape.com/"),
    ("JSON API", "https://httpbin.org/json"),
]


def show(wc: WebClient, label: str, url: str) -> None:
    print(f"\n=== {label}: {url} ===")
    summary = wc.fetch(url).summary()
    # exclude_none keeps the view lean -- only facets that apply appear.
    data = summary.model_dump(exclude_none=True)
    # trim the two potentially-long lists so the print stays readable
    if summary.structure:
        data["structure"]["toc"] = data["structure"]["toc"][:3]
        data["structure"]["link_sample"] = data["structure"]["link_sample"][:3]
    print(json.dumps(data, indent=2, default=str)[:1400])


def main() -> None:
    with WebClient(default_headers={"User-Agent": UA}, min_interval=0.3, timeout=25) as wc:
        for label, url in PAGES:
            try:
                show(wc, label, url)
            except WebException as exc:
                print(f"  {label}: {exc.error.type} (skipped)")


if __name__ == "__main__":
    main()
