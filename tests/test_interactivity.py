"""System B -- interactivity detection (the pure static/semantic tier).

Capture-time (listener / cursor / scroll) detection is a later seam; here we prove the
static detector and the merge seam on parsed elements, no browser.
"""

from lxml import html as _lh

from webclient.core.document.interactivity import (
    DomInteractive,
    interactive,
    merge,
)


def _el(markup: str):
    return _lh.fragment_fromstring(markup)


def test_native_controls_are_click_targets():
    assert interactive(_el('<button>Go</button>')).click
    assert interactive(_el('<a href="/x">More</a>')).click
    assert interactive(_el('<input type="text">')).click
    assert interactive(_el('<select><option>a</option></select>')).click
    assert interactive(_el('<summary>Details</summary>')).click


def test_a_without_href_is_not_a_control():
    assert interactive(_el('<a>anchor target</a>')) is None


def test_plain_containers_are_not_interactive():
    assert interactive(_el('<div>just text</div>')) is None
    assert interactive(_el('<span class="label">x</span>')) is None


def test_aria_role_makes_a_div_a_control():
    got = interactive(_el('<div role="button">Read more</div>'))
    assert got is not None and got.click and got.source == "semantic"


def test_onclick_tabindex_contenteditable_are_controls():
    assert interactive(_el('<div onclick="go()">x</div>')).click
    assert interactive(_el('<div tabindex="0">x</div>')).click
    assert interactive(_el('<div contenteditable="true">x</div>')).click


def test_node_key_is_read_from_the_stamp_when_present():
    got = interactive(_el('<button data-wc-node="n7">Go</button>'))
    assert got is not None and got.node_key == "n7"


def test_merge_ors_static_and_dynamic_findings():
    static = {"n1": DomInteractive(node_key="n1", click=True, source="semantic")}
    dynamic = {
        "n1": DomInteractive(node_key="n1", hover=True, source="cursor"),
        "n2": DomInteractive(node_key="n2", scroll=True, source="scrollable"),
    }
    out = merge(static, dynamic)
    assert out["n1"].click and out["n1"].hover and out["n1"].source == "semantic+cursor"
    assert out["n2"].scroll and "n1" in out and "n2" in out


def test_bool_reflects_any_interactivity():
    assert not DomInteractive()
    assert DomInteractive(click=True)


def test_skeleton_marks_only_non_obvious_controls_and_is_toggleable():
    from webclient.core.document import Document

    html = (
        b"<html><body>"
        b'<div role="button">Expand</div>'  # non-obvious -> marked
        b'<a href="/x">link</a>'  # obvious -> NOT marked
        b"<button>Btn</button>"  # obvious -> NOT marked
        b'<span onclick="go()">tap</span>'  # non-obvious -> marked
        b"</body></html>"
    )
    d = Document(content=html, kind="html", status_code=200)
    sk = d.skeleton()
    assert sk.count("← clickable") == 2  # only the div[role] and span[onclick]
    assert "← clickable" not in d.skeleton(mark_interactive=False)  # toggle off
