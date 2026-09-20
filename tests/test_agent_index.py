"""Phase 4 -- the interaction loop targets controls BY INDEX. A policy returns
``Click(index=N)``; the loop resolves the index to the durable selector from the observed
element table, so it executes and RECORDS a normal selector action (replayable). These are the
fast, browser-free unit tests of the resolution + freshness skip + observation table."""

from webclient.core.document import Document
from webclient.core.document.models import IndexedElement
from webclient.llm.agent import (
    Click, Done, Goto, Observation, Type, _apply, _observe, _resolve,
)


def _obs(elements):
    return Observation(step=0, max_steps=5, elements=elements)


def test_resolve_maps_an_index_to_its_durable_selector():
    els = [
        IndexedElement(index=1, role="button", name="Go", selector="#go"),
        IndexedElement(index=2, role="textbox", name="Q", selector='input[name="q"]'),
    ]
    obs = _obs(els)
    assert _resolve(Click(index=1), obs).selector == "#go"
    typed = _resolve(Type(index=2, text="hi"), obs)
    assert typed.selector == 'input[name="q"]' and typed.text == "hi"


def test_resolve_passes_selector_and_no_target_actions_through():
    obs = _obs([])
    assert _resolve(Click(selector=".x"), obs).selector == ".x"  # an explicit selector is kept
    assert _resolve(Goto(url="http://x/"), obs).url == "http://x/"  # no index -> unchanged
    assert isinstance(_resolve(Done(result="ok"), obs), Done)


def test_unknown_index_resolves_to_no_selector_and_apply_skips_it():
    obs = _obs([IndexedElement(index=1, role="button", selector="#go")])
    act = _resolve(Click(index=9), obs)  # an index not in the table (stale/gone)
    assert act.selector == ""

    class FakeDoc:
        def click(self, *a):
            raise AssertionError("must not click a stale/unresolved index")

    _apply(FakeDoc(), act)  # no exception -> the freshness guard skipped it


def test_observe_populates_the_interactive_element_table():
    doc = Document(
        url="http://x/", kind="html", status_code=200,
        content=b'<html><body><button id="go">Go</button>'
                b'<a href="/x">Link</a></body></html>',
    )
    obs = _observe(doc, 0, 5, "")
    roles = {e.role for e in obs.elements}
    assert "button" in roles and "link" in roles
    assert any(e.selector == "#go" for e in obs.elements)
