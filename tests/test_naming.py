"""System C -- human-readable element names (the pure static namer)."""

from lxml import html as _lh

from webclient.core.document.naming import DomName, name


def _el(markup: str):
    return _lh.fragment_fromstring(markup)


def test_aria_label_wins():
    got = name(_el('<button aria-label="Close dialog">×</button>'))
    assert got is not None and got.label == "Close dialog" and got.source == "aria"


def test_alt_then_title_then_placeholder():
    assert name(_el('<img alt="Red mug" src="x.png">')).label == "Red mug"
    assert name(_el('<span title="Next page">›</span>')).label == "Next page"
    assert name(_el('<input placeholder="Search products">')).label == "Search products"


def test_button_value_is_a_label():
    assert name(_el('<input type="submit" value="Sign in">')).label == "Sign in"


def test_short_visible_text_is_the_label():
    got = name(_el('<a href="/x">Read more</a>'))
    assert got is not None and got.label == "Read more" and got.source == "text"


def test_word_like_id_is_humanized():
    got = name(_el('<div id="readMore-btn"></div>'))
    assert got is not None and got.label == "read more btn" and got.source == "id"


def test_hashed_id_yields_no_name():
    # better none than noise: an opaque/build-hash id is not a human name.
    assert name(_el('<div id="css-1a2b3c"></div>')) is None
    assert name(_el('<div id="x7f3a9b2"></div>')) is None


def test_plain_container_with_long_text_has_no_name():
    long = "This is a very long paragraph of body copy that is not a label at all, well beyond"
    assert name(_el(f'<div>{long}</div>')) is None


def test_node_key_is_carried_from_the_stamp():
    got = name(_el('<button data-wc-node="n3" aria-label="Menu">≡</button>'))
    assert got is not None and got.node_key == "n3"


def test_bool():
    assert not DomName()
    assert DomName(label="x")
