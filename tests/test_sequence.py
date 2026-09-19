"""Feature A: sequence-valued lazy plans -- a multi-step script against ONE live
page as a SINGLE plan. ``.step(action)`` chains an ACTION (wait_for/click/write)
into an ordered sequence rooted at one resolved doc; ``.extract(...)`` is the
capture (it accumulates named fields onto the doc's row); ``.project()`` renders
them. The executor resolves the root page ONCE, holds it, replays each step
against the SAME live doc, and releases the page only when the sequence finishes.

One WebClient (one chromium) for the module -- the same pattern as test_browser.
"""

import pytest

from webclient import WebClient, wq

# a page whose two buttons each reveal a distinct span on click -- so a sequence
# must click, read, click again, read again, all against ONE held page.
STEPS_APP = """
<html><head><title>Steps</title></head><body>
  <div id="out-a"></div>
  <div id="out-b"></div>
  <button id="reveal-a"
    onclick="document.getElementById('out-a').innerHTML='<span class=va>ALPHA</span>'">A</button>
  <button id="reveal-b"
    onclick="document.getElementById('out-b').innerHTML='<span class=vb>BETA</span>'">B</button>
</body></html>
"""


@pytest.fixture(scope="module")
def wc():
    with WebClient(timeout=10.0) as client:
        yield client


@pytest.fixture
def url(httpserver):
    httpserver.expect_request("/steps").respond_with_data(
        STEPS_APP, content_type="text/html"
    )
    return httpserver.url_for("/steps")


def test_sequence_clicks_extracts_clicks_extracts_from_one_page(wc, url):
    # click reveal-a -> extract a; click reveal-b -> extract b; project both.
    # Both fields come from the SAME held page across the interleaved steps.
    seq = (
        wq.ref.resolve(browser="always")
        .step(wq.doc.click("#reveal-a"))
        .step(wq.doc.wait_for(".va"))
        .extract(a=wq.doc.select(".va").attr("text"))
        .step(wq.doc.click("#reveal-b"))
        .step(wq.doc.wait_for(".vb"))
        .extract(b=wq.doc.select(".vb").attr("text"))
        .project()
    )
    row = wc.execute(seq, wc.ref(url))
    assert row == {"a": "ALPHA", "b": "BETA"}


def test_sequence_is_one_plan_with_ordered_step_ops(url):
    # the whole script is ONE recorded plan: an ordered list of steps, the actions
    # recorded as `step` ops interleaved with the `extract` captures.
    seq = (
        wq.ref.resolve(browser="always")
        .step(wq.doc.click("#reveal-a"))
        .extract(a=wq.doc.select(".va").attr("text"))
        .project()
    )
    names = [(s.kind, s.name) for s in seq._plan.steps]
    assert ("get", "step") in names
    assert names.count(("get", "step")) == 1
    # it round-trips through the readable form like any other plan
    from webclient import from_describe

    assert [(s.kind, s.name) for s in from_describe(seq.describe())._plan.steps] == names


def test_page_is_held_across_the_sequence_and_released_at_the_end(wc, url):
    # the core requirement: the executor must NOT release the page mid-sequence
    # (each step interacts with the live DOM), and MUST release it when the
    # sequence completes -- the sequence owns the page (no keep_alive handle).
    before = wc.pool._held.get("page", 0)

    # a step that asserts, mid-sequence, that the page is STILL leased. The probe
    # runs as an extract column against the live doc -- if the page were released,
    # its live click below would fail and the DOM would be frozen.
    seq = (
        wq.ref.resolve(browser="always")
        .step(wq.doc.click("#reveal-a"))
        .extract(mid_held=wq.doc.evaluate("!!document.getElementById('reveal-b')"))
        .step(wq.doc.click("#reveal-b"))  # only works if the page is still live
        .step(wq.doc.wait_for(".vb"))
        .extract(b=wq.doc.select(".vb").attr("text"))
        .project()
    )
    during = wc.pool._held.get("page", 0)  # nothing running yet
    row = wc.execute(seq, wc.ref(url))
    after = wc.pool._held.get("page", 0)

    assert row["b"] == "BETA"  # the second click landed -> page was live throughout
    assert row["mid_held"] is True
    assert during == before  # baseline
    assert after == before  # the sequence released its page at the end -- no leak


def test_remote_allows_a_complete_sequence_but_rejects_an_open_one():
    # a COMPLETE stepful plan (ends in .project()) produces DATA: its held page lives
    # entirely inside one server-side /execute and never crosses the wire, so it runs
    # remotely like any browser plan. An OPEN one (returns a live page/element) stays
    # engine-local and fails clearly, telling the caller to close it with .project().
    from webclient.core.remote import _reject_sequence

    complete = (
        wq.ref.resolve(browser="always")
        .step(wq.doc.click("#reveal-a"))
        .extract(a=wq.doc.select(".va").attr("text"))
        .project()
    )
    _reject_sequence(complete)  # does not raise -- a complete sequence runs remotely

    open_seq = (
        wq.ref.resolve(browser="always")
        .step(wq.doc.click("#reveal-a"))
        .select_all(".va")  # returns a live selection, not data
    )
    with pytest.raises(NotImplementedError):
        _reject_sequence(open_seq)

    # an ordinary (non-sequence) plan is untouched by the guard
    _reject_sequence(wq.ref.resolve().select_all(".card").project())  # does not raise


def test_sequence_release_survives_repeats_without_leaking(wc, url):
    # run the sequence several times; each run must return its page, so the held
    # count returns to baseline every time (scope-owned release, v1).
    before = wc.pool._held.get("page", 0)
    for _ in range(3):
        seq = (
            wq.ref.resolve(browser="always")
            .step(wq.doc.click("#reveal-a"))
            .step(wq.doc.wait_for(".va"))
            .extract(a=wq.doc.select(".va").attr("text"))
            .project()
        )
        assert wc.execute(seq, wc.ref(url)) == {"a": "ALPHA"}
    assert wc.pool._held.get("page", 0) == before
