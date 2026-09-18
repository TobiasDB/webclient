"""Feature B: plan/expr visualization -- the two read-only renderers over the
``Plan`` IR (``webclient.query.viz`` + the thin ``Expr.explain_tree()`` /
``Expr.wireframe()`` methods). Built via the ``wq`` recorder, read off
``expr._plan`` -- never executed."""

from webclient import wq
from webclient.query.viz import explain, wireframe


def _representative():
    return (
        wq.ref.resolve()
        .select_all(".card")
        .extract(
            title=wq.doc.select("h3").attr("text"),
            url=wq.doc.select("a").attr("href"),
        )
        .project()
    )


def test_explain_is_an_indented_op_tree_with_args():
    q = _representative()
    text = explain(q._plan)
    # the op headline lines, in order down the spine
    assert "RESOLVE" in text
    assert "SELECT_ALL  .card  (fan-out)" in text
    assert "EXTRACT" in text
    assert "PROJECT" in text
    # extract columns hang below EXTRACT as field branches, with their sub-selector
    assert "title = select('h3').attr('text')" in text
    assert "url = select('a').attr('href')" in text
    # indentation deepens down the pipeline (a spine), and uses tree connectors
    lines = text.splitlines()
    assert lines[0] == "Reference"  # the root label
    assert "└─" in text and "├─" in text
    assert lines[1].index("└─") < lines[2].index("└─")


def test_explain_via_expr_method_matches_the_free_function():
    q = _representative()
    assert q.explain_tree() == explain(q._plan)
    # the one-line explain() is unchanged (still the round-trippable describe form)
    assert q.explain() == q._plan.describe()
    assert "\n" not in q.explain()  # one line
    assert "\n" in q.explain_tree()  # a tree


def test_explain_reference_root_and_link_follow_recursion():
    q = (
        wq.reference("https://site/news")
        .resolve(browser="auto")
        .select_all("li.item")
        .extract(
            title=wq.doc.select("h3").attr("text"),
            price=wq.doc.select("a").attr("href").resolve().select(".price").attr("text"),
        )
        .project()
    )
    text = explain(q._plan)
    assert text.splitlines()[0] == "reference('https://site/news')"
    assert "browser=auto" in text
    assert "li.item" in text
    # a link-follow column (attr('href').resolve()) recurses to a per-row detail page
    assert "RESOLVE (per row)" in text


def test_wireframe_is_self_contained_html_with_the_visual_grammar():
    q = _representative()
    html = wireframe(q._plan)
    # self-contained: a full HTML doc, inline CSS/SVG, no external deps
    assert html.startswith("<!doctype html>")
    assert "<style>" in html and "cdn" not in html.lower()
    assert "http://" not in html.replace('xmlns="http://www.w3.org/2000/svg"', "")
    # the visual grammar: a page frame, a fan-out box with a ghost duplicate,
    # field chips, and an output card
    assert "wc-frame" in html and "wc-chrome" in html
    assert "wc-selectall" in html and "wc-ghost" in html
    assert "× many" in html  # the select_all ghost annotation
    assert "wc-chip" in html
    assert "<b>title</b>" in html and "<b>url</b>" in html  # the field chips
    assert "wc-output" in html  # the project card


def test_wireframe_frames_the_source_and_nests_a_link_follow():
    q = (
        wq.reference("https://shop/list")
        .resolve()
        .select_all(".item")
        .extract(link=wq.doc.select("a").attr("href").resolve().select(".p").attr("text"))
        .project()
    )
    html = wireframe(q._plan)
    # the root frame is titled with the source
    assert "https://shop/list" in html
    # a link-follow column emits an arrow (inline SVG) to a NESTED page frame
    assert "wc-nested" in html and "wc-arrow" in html
    assert html.count("wc-frame") >= 2  # the root frame + the detail frame
    assert "<svg" in html  # the arrow is inline SVG


def test_wireframe_renders_a_filter_predicate_badge():
    q = (
        wq.ref.resolve()
        .select_all(".card")
        .filter(wq.doc.select(".price").attr("text") != "")
        .extract(name=wq.doc.select("h3").attr("text"))
        .project()
    )
    html = wireframe(q._plan)
    assert "wc-badge" in html and "filter:" in html
    text = explain(q._plan)
    assert "FILTER" in text
