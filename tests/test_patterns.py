"""Web patterns PoC (roadmap N11): DOM recon hints for extract / interact / crawl, registered
like signals, surfaced as the ``patterns`` facet."""

from webclient import WebClient
from webclient.dom import parse_html
from webclient.patterns import PATTERNS, PatternContext, PatternHint, detect, pattern, template_signature

SHOP = """<html><body><nav><a href="/a">a</a><a href="/b">b</a><a href="/c">c</a></nav>
<main><h1>Shop</h1>
<ul class="grid">
 <li class="card"><h2 class="t">One</h2><span class="p">$1</span><button class="buy">Add to cart</button></li>
 <li class="card"><h2 class="t">Two</h2><span class="p">$2</span><button class="buy">Add to cart</button></li>
 <li class="card"><h2 class="t">Three</h2><span class="p">$3</span><button class="buy">Add to cart</button></li>
 <li class="card"><h2 class="t">Four</h2><span class="p">$4</span><button class="buy">Add to cart</button></li>
</ul></main><footer>f</footer></body></html>"""


def test_record_list_and_repeated_control_hints():
    hints = detect(PatternContext(tree=parse_html(SHOP)))
    by = {}
    for h in hints:  # the FIRST (most confident) hint per pattern
        by.setdefault(h.name, h)
    rec = by["record_list"]
    assert rec.subject == "li.card" and rec.count == 4 and rec.for_ == ("extract",) and rec.confidence == 1.0
    ctl = by["repeated_control"]
    assert ctl.count == 4 and ctl.for_ == ("interact",) and ctl.value == {"label": "add to cart"}
    assert ctl.subject.startswith("button")
    tpl = by["page_template"]
    assert tpl.for_ == ("crawl",) and len(tpl.subject) == 16 and "<main>" in tpl.value["signature"]
    assert [h.name for h in detect(PatternContext(tree=parse_html(SHOP)), for_="crawl")] == ["page_template"]


def test_template_signature_ignores_data_but_not_structure():
    a = template_signature(parse_html(SHOP))
    b = template_signature(parse_html(SHOP.replace("One", "Uno").replace("$1", "$9")))
    c = template_signature(parse_html(SHOP.replace("<footer>f</footer>", "<footer>f</footer><aside>x</aside>")))
    assert a == b and a != c


def test_patterns_are_extensible_and_advisory():
    @pattern("boom", for_=("crawl",))
    def _boom(ctx):
        raise RuntimeError("never fatal")

    @pattern("has_nav", for_=("crawl",))
    def _nav(ctx):
        if ctx.tree is not None and ctx.tree.cssselect("nav"):
            yield PatternHint(name="", subject="nav", count=1, confidence=0.9)

    try:
        hints = detect(PatternContext(tree=parse_html(SHOP)), for_="crawl")
        assert [h.name for h in hints][:2] == ["page_template", "has_nav"]
        assert hints[1].for_ == ("crawl",)
    finally:
        PATTERNS[:] = [p for p in PATTERNS if p.name not in ("boom", "has_nav")]


def test_patterns_facet_on_a_document(httpserver):
    httpserver.expect_request("/shop").respond_with_data(SHOP, content_type="text/html")
    with WebClient() as wc:
        doc = wc.fetch(httpserver.url_for("/shop"))
        hints = doc.patterns()
        assert hints[0].name in ("record_list", "page_template") and any(h.subject == "li.card" for h in hints)
        assert [h.name for h in doc.patterns(for_="interact")] == ["repeated_control"]
        assert doc.patterns()[0].confidence <= 1.0
        rows = doc.select_all(doc.patterns(for_="extract")[0].subject).extract(t=None).project() if False else None
