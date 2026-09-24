"""Web patterns: recurring-STRUCTURE signals -- the record list(s) to extract, the repeated controls
to act on per item, and the page template. They are Signals/Flags now (the ``record_regions`` /
``repeated_controls`` / ``page_template`` flags), read via ``doc.patterns(for_=...)`` or the per-flag
accessors -- NOT a parallel registry."""

from webclient import WebClient
from webclient.dom import parse_html
from webclient.signals import Context, flags
from webclient.signals.patterns import template_signature

SHOP = """<html><body><nav><a href="/a">a</a><a href="/b">b</a><a href="/c">c</a></nav>
<main><h1>Shop</h1>
<ul class="grid">
 <li class="card"><h2 class="t">One</h2><span class="p">$1</span><button class="buy">Add to cart</button></li>
 <li class="card"><h2 class="t">Two</h2><span class="p">$2</span><button class="buy">Add to cart</button></li>
 <li class="card"><h2 class="t">Three</h2><span class="p">$3</span><button class="buy">Add to cart</button></li>
 <li class="card"><h2 class="t">Four</h2><span class="p">$4</span><button class="buy">Add to cart</button></li>
</ul></main><footer>f</footer></body></html>"""


def _flags_of(html: str):
    # patterns are the "pattern" flag group (structural), kept out of the conclusion set
    return flags(Context.from_response(200, {"content-type": "text/html"}, {}, html.encode()), group="pattern")


def test_pattern_flags_detect_record_list_control_and_template():
    fl = _flags_of(SHOP)
    rec = fl["record_regions"].value[0]  # the most confident record region
    assert rec.subject == "li.card" and rec.count == 4 and rec.for_ == ("extract",) and rec.confidence == 1.0
    ctl = fl["repeated_controls"].value[0]
    assert ctl.count == 4 and ctl.for_ == ("interact",) and ctl.value == {"label": "add to cart"}
    assert ctl.subject.startswith("button")
    tpl = fl["page_template"].value[0]
    assert tpl.for_ == ("crawl",) and len(tpl.subject) == 16 and "<main>" in tpl.value["signature"]


def test_template_signature_ignores_data_but_not_structure():
    a = template_signature(parse_html(SHOP))
    b = template_signature(parse_html(SHOP.replace("One", "Uno").replace("$1", "$9")))
    c = template_signature(parse_html(SHOP.replace("<footer>f</footer>", "<footer>f</footer><aside>x</aside>")))
    assert a == b and a != c


def test_patterns_read_via_the_document(httpserver):
    httpserver.expect_request("/shop").respond_with_data(SHOP, content_type="text/html")
    with WebClient() as wc:
        doc = wc.fetch(httpserver.url_for("/shop"))
        hints = doc.patterns()  # every kind, most confident first
        assert any(h.subject == "li.card" for h in hints) and hints[0].confidence <= 1.0
        extract = doc.patterns(for_="extract")
        assert extract and all(h.name == "record_list" for h in extract) and any(h.subject == "li.card" for h in extract)
        interact = doc.patterns(for_="interact")
        assert interact and all(h.name == "repeated_control" for h in interact)
        # the per-flag accessor carries the same hints (patterns ARE flags)
        assert doc.record_regions().value[0].subject == "li.card"
        assert doc.page_template().value[0].name == "page_template"
