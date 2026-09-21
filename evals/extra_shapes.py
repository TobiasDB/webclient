"""A handful of CLEAN, well-formed corpus shapes (semantic classes, no utility-class noise).

These isolate whether the index primitive works on well-structured pages -- the opposite end
from the messy_html scenarios. If these PASS and the messy ones FAIL, the gap is robustness to
real-world class soup, not the core algorithm.
"""

from __future__ import annotations

# imported lazily by the harness to avoid a hard import cycle at module load
from query_engine_eval import Page  # type: ignore  # noqa: E402


def _ul_list() -> Page:
    html = """<html><body>
      <header><nav><a href="/">Home</a><a href="/about">About</a></nav></header>
      <main><ul class="results">
        <li class="card"><h3 class="title">Aeropress</h3><span class="price">$39</span></li>
        <li class="card"><h3 class="title">Grinder</h3><span class="price">$59</span></li>
        <li class="card"><h3 class="title">Kettle</h3><span class="price">$79</span></li>
        <li class="card"><h3 class="title">Scale</h3><span class="price">$25</span></li>
      </ul></main></body></html>"""
    expected = [{"title": t, "price": p} for t, p in
                [("Aeropress", "$39"), ("Grinder", "$59"), ("Kettle", "$79"), ("Scale", "$25")]]
    return Page("clean_ul_list", html, "html", ["title", "price"], expected,
                "a clean <ul> card list with semantic classes and a nav decoy")


def _table() -> Page:
    rows = [("Q1 2026", "$1.20B"), ("Q2 2026", "$1.45B"), ("Q3 2026", "$0.90B")]
    body = "".join(f'<tr class="row"><td class="q">{q}</td><td class="rev">{r}</td></tr>'
                   for q, r in rows)
    html = f"""<html><body><main>
      <table class="results"><thead><tr><th>Quarter</th><th>Revenue</th></tr></thead>
      <tbody>{body}</tbody></table></main></body></html>"""
    expected = [{"quarter": q, "revenue": r} for q, r in rows]
    return Page("clean_table", html, "html", ["quarter", "revenue"], expected,
                "a clean HTML table, no interleaved header/total rows")


def _card_grid() -> Page:
    data = [("Nimbus", "Router", "$129"), ("Cirrus", "Switch", "$349"), ("Stratus", "AP", "$99")]
    cards = "".join(
        f'<div class="card"><h2 class="name">{n}</h2>'
        f'<span class="kind">{k}</span><span class="price">{p}</span></div>'
        for n, k, p in data)
    html = f'<html><body><main><div class="grid">{cards}</div></main></body></html>'
    expected = [{"name": n, "kind": k, "price": p} for n, k, p in data]
    return Page("clean_card_grid", html, "html", ["name", "kind", "price"], expected,
                "a clean div.card grid with three text fields")


def _two_regions() -> Page:
    # two DISTINCT record regions on one page; the dataset is the products, the sidebar is a decoy.
    prods = "".join(f'<li class="product"><span class="name">P{i}</span>'
                    f'<span class="price">${i}0</span></li>' for i in range(5))
    related = "".join(f'<li class="related-item"><a href="/r{i}">Related {i}</a></li>'
                      for i in range(4))
    html = f"""<html><body>
      <main><ul class="catalog">{prods}</ul></main>
      <aside><ul class="related">{related}</ul></aside></body></html>"""
    expected = [{"name": f"P{i}", "price": f"${i}0"} for i in range(5)]
    return Page("clean_two_regions", html, "html", ["name", "price"], expected,
                "two record regions: the 5-item product list (dataset) vs a 4-item related sidebar")


def _nested_record() -> Page:
    # each record has a nested author block; the brief wants flat title + author name.
    posts = [("Alpha", "Ada"), ("Beta", "Grace"), ("Gamma", "Linus")]
    cards = "".join(
        f'<article class="post"><h3 class="title">{t}</h3>'
        f'<div class="byline"><span class="author">{a}</span></div></article>'
        for t, a in posts)
    html = f'<html><body><main>{cards}</main></body></html>'
    expected = [{"title": t, "author": a} for t, a in posts]
    return Page("clean_nested_record", html, "html", ["title", "author"], expected,
                "each record wraps its author in a nested block; brief wants flat title+author")


def pages() -> "list[Page]":
    return [_ul_list(), _table(), _card_grid(), _two_regions(), _nested_record()]
