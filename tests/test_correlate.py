"""The XHR->DOM correlation seam: the cheap OrderingCorrelator, from synthetic events.

No browser -- feed NetworkEvent/DOMUpdateEvent straight in and assert the Correlation.
This is the frozen interface a richer (content-matching) Correlator must also satisfy.
"""

import json

from webclient.core.document.correlate import (
    ContentCorrelator,
    Correlation,
    Correlator,
    DomPhase,
    OrderingCorrelator,
    XhrRequest,
)
from webclient.core.reference import from_url
from webclient.models import DOMUpdateEvent, NetworkEvent


def _net(index, url, t_s, method="GET"):
    return NetworkEvent(
        request=from_url(url, method.lower()), resource_type="xhr",
        method=method, index=index, t_s=t_s,
    )


def _mut(node, xhr_index, t_s, kind="added", action=0):
    return DOMUpdateEvent(
        kind=kind, node_id=node,
        detail={"xhr_index": xhr_index, "action": action, "t_s": t_s},
    )


def test_ordering_correlator_conforms_to_the_protocol():
    assert isinstance(OrderingCorrelator(), Correlator)


def test_a_node_is_attributed_to_the_request_completed_before_it():
    net = [_net(1, "https://x/api/config", 0.0), _net(2, "https://x/api/news", 0.22)]
    dom = [_mut("n1", xhr_index=2, t_s=0.24)]  # mutated after request 2 completed
    corr = OrderingCorrelator().correlate(net, dom)
    assert [r.url for r in corr.requests] == ["https://x/api/config", "https://x/api/news"]
    assert corr.candidates_for("n1") == [2]
    assert corr.request(2) is not None and corr.request(2).t_s == 0.22


def test_pre_xhr_mutation_has_no_candidates():
    # a node that mutated before ANY request completed is server-static / pure JS, not xhr.
    corr = OrderingCorrelator().correlate([_net(1, "https://x/api", 0.5)], [_mut("n1", 0, 0.1)])
    assert corr.candidates_for("n1") == []  # empty -> definitely not from an xhr


def test_near_simultaneous_requests_are_both_candidates():
    # requests 2 and 3 complete 13ms apart; a node mutated just after can't be pinned to one.
    net = [_net(1, "https://x/a", 0.0), _net(2, "https://x/b", 0.34), _net(3, "https://x/c", 0.353)]
    corr = OrderingCorrelator(window_s=0.05).correlate(net, [_mut("n1", 3, 0.36)])
    assert corr.candidates_for("n1") == [2, 3]  # ambiguity preserved, not falsely resolved


def test_a_distant_earlier_request_is_not_a_candidate():
    net = [_net(1, "https://x/a", 0.0), _net(2, "https://x/b", 0.34), _net(3, "https://x/c", 0.353)]
    corr = OrderingCorrelator(window_s=0.05).correlate(net, [_mut("n1", 2, 0.35)])
    assert corr.candidates_for("n1") == [2]  # request 1 is 340ms earlier -> not co-responsible


def test_a_node_that_re_renders_merges_every_phase_it_passed_through():
    net = [_net(1, "https://x/a", 0.0), _net(2, "https://x/b", 0.5)]
    dom = [_mut("n1", 1, 0.1), _mut("n1", 2, 0.6)]  # same node, two mutations
    corr = OrderingCorrelator().correlate(net, dom)
    assert corr.candidates_for("n1") == [1, 2]
    assert corr.request(2) is not None  # the later mutation's request is present


def test_a_node_is_attributed_to_the_action_that_first_revealed_it():
    # a .click() bumps the action counter; DOM that then appears carries that action index.
    net: list = []
    dom = [_mut("n1", xhr_index=0, t_s=1.0, action=2)]  # appeared after the 2nd interaction
    corr = OrderingCorrelator().correlate(net, dom)
    assert corr.action_for("n1") == 2
    assert corr.candidates_for("n1") == []  # no xhr involved -> action-driven only


def test_action_keeps_the_earliest_appearance_not_a_later_re_render():
    dom = [_mut("n1", 0, 1.0, action=1), _mut("n1", 0, 2.0, action=3)]  # appeared at action 1
    corr = OrderingCorrelator().correlate([], dom)
    assert corr.action_for("n1") == 1


def test_mutations_without_a_node_id_are_ignored():
    corr = OrderingCorrelator().correlate([_net(1, "https://x/a", 0.0)], [_mut("", 1, 0.1)])
    assert corr.phases == []  # no stable node -> no phantom phase


def test_skeleton_annotates_phase_and_never_shows_the_stamp():
    # the end-to-end wiring, no browser: a captured page (data-wc-node stamps in the HTML)
    # + a fake PageResult carrying the xhr timeline + phase stamps -> the skeleton lists the
    # requests and annotates the record region "← after req[1]", but never leaks data-wc-node.
    from webclient.clients.browser import PageResult
    from webclient.core.document import Document
    from webclient.core.document.live import LiveBacking

    html = (
        b'<html><body><ul class="news">'
        b'<li class="item" data-wc-node="n1">Story A</li>'
        b'<li class="item" data-wc-node="n2">Story B</li>'
        b"</ul></body></html>"
    )
    doc = Document(content=html, kind="html", status_code=200)
    result = PageResult(
        "http://x/", html,
        xhr=[{"index": 1, "method": "GET", "url": "http://x/api/news", "t": 0.22}],
        stamps=[{"node": "n1", "xhr": 1, "t": 0.24}, {"node": "n2", "xhr": 1, "t": 0.24}],
    )
    LiveBacking().on_load(doc, result)

    sk = doc.skeleton()
    assert "http://x/api/news" in sk and "0.22s" in sk  # the request timeline (relative seconds)
    assert "← after req[1]" in sk  # the record region attributed to request 1
    assert "data-wc-node" not in sk  # the internal stamp is never shown (never a selector)
    assert "data-wc-node" not in doc.attr("html")  # nor leaked into rendered output


def test_models_round_trip():
    c = Correlation(
        requests=[XhrRequest(index=1, url="https://x/a", t_s=0.0)],
        phases=[DomPhase(node_key="n1", candidates=[1], t_s=0.1)],
    )
    assert Correlation.model_validate(c.model_dump()) == c


# --------------------------------------------------------------------------- #
# ContentCorrelator: refine the ordering baseline by response-body value matching
# --------------------------------------------------------------------------- #

GUID_A = "550e8400-e29b-41d4-a716-446655440000"  # high-entropy value uniquely in body A
GUID_B = "6ba7b810-9dad-11d1-80b4-00c04fd430c8"  # ...and in body B


def _netb(index, url, t_s, body, method="GET"):
    """A NetworkEvent carrying a response ``body`` (what ContentCorrelator value-matches)."""
    return NetworkEvent(
        request=from_url(url, method.lower()), resource_type="xhr",
        method=method, index=index, t_s=t_s,
        body=(body.encode() if isinstance(body, str) else body),
    )


def _mutt(node, xhr_index, t_s, text, action=0):
    """A DOM stamp carrying the node's text snippet (the content side of the match)."""
    return DOMUpdateEvent(
        kind="added", node_id=node,
        detail={"xhr_index": xhr_index, "action": action, "t_s": t_s, "text": text},
    )


def test_content_correlator_conforms_to_the_protocol():
    assert isinstance(ContentCorrelator(), Correlator)


def test_content_narrows_an_ambiguous_ordering_result_by_a_high_entropy_value_match():
    # requests 2 and 3 complete near-simultaneously -> ORDERING keeps both as candidates. The
    # node's text carries GUID_A, which appears ONLY in request 2's body -> content narrows to [2].
    net = [
        _netb(1, "https://x/boot", 0.0, "{}"),
        _netb(2, "https://x/order", 0.34, json.dumps({"id": GUID_A, "label": "Order confirmed"})),
        _netb(3, "https://x/other", 0.353, json.dumps({"id": GUID_B, "label": "Other thing"})),
    ]
    dom = [_mutt("n1", xhr_index=3, t_s=0.36, text=f"Order {GUID_A} confirmed")]

    ordering = OrderingCorrelator(window_s=0.05).correlate(net, dom)
    assert ordering.candidates_for("n1") == [2, 3]  # baseline is genuinely ambiguous

    content = ContentCorrelator(window_s=0.05).correlate(net, dom)
    assert content.candidates_for("n1") == [2]  # value match dominates -> narrowed
    phase = next(p for p in content.phases if p.node_key == "n1")
    assert phase.confidence is not None and phase.confidence > 0.5


def test_content_preserves_ambiguity_when_the_content_is_genuinely_ambiguous():
    # both candidate bodies echo the SAME id -> the value match cannot separate them, so the
    # ambiguity is PRESERVED, not falsely resolved.
    net = [
        _netb(2, "https://x/a", 0.34, json.dumps({"id": GUID_A})),
        _netb(3, "https://x/b", 0.353, json.dumps({"id": GUID_A})),
    ]
    dom = [_mutt("n1", xhr_index=3, t_s=0.36, text=f"Item {GUID_A}")]
    content = ContentCorrelator(window_s=0.05).correlate(net, dom)
    assert content.candidates_for("n1") == [2, 3]  # no clear winner -> kept ambiguous


def test_content_does_not_narrow_on_a_low_specificity_common_word():
    # the only shared token is a short, common word -> below the specificity floor, no narrowing.
    net = [
        _netb(2, "https://x/a", 0.34, json.dumps({"body": "the news of the day"})),
        _netb(3, "https://x/b", 0.353, json.dumps({"body": "an unrelated payload"})),
    ]
    dom = [_mutt("n1", xhr_index=3, t_s=0.36, text="the news")]
    content = ContentCorrelator(window_s=0.05).correlate(net, dom)
    assert content.candidates_for("n1") == [2, 3]  # a coincidental common-word match is not enough


def test_content_never_widens_the_temporal_gate():
    # the node mutated when only request 1 had completed (xhr_index=1). Request 2 completes LATER
    # and its body DOES contain the node's value -- but a DOM update cannot precede its cause, so
    # request 2 must NEVER become a candidate. The ordering gate is absolute.
    net = [
        _netb(1, "https://x/a", 0.0, "{}"),
        _netb(2, "https://x/late", 0.9, json.dumps({"id": GUID_A})),
    ]
    dom = [_mutt("n1", xhr_index=1, t_s=0.1, text=f"Order {GUID_A}")]
    content = ContentCorrelator().correlate(net, dom)
    assert content.candidates_for("n1") == [1]  # NOT [2], and NOT [1, 2]


def test_content_correlator_wires_through_the_skeleton_under_the_env_var(monkeypatch):
    # end-to-end (no browser): a captured page whose PageResult carries the XHR timeline, the
    # response BODIES, and phase stamps with node text -> under WEBCLIENT_CORRELATOR=content the
    # skeleton narrows the ambiguous "← after req[2, 3]" to "← after req[2]" by the value match.
    from webclient.clients.browser import PageResult
    from webclient.core.document import Document
    from webclient.core.document.live import LiveBacking

    html = (
        b'<html><body><ul class="news">'
        b'<li class="item" data-wc-node="n1">Order ' + GUID_A.encode() + b" confirmed</li>"
        b"</ul></body></html>"
    )
    doc = Document(content=html, kind="html", status_code=200)
    result = PageResult(
        "http://x/", html,
        xhr=[
            {"index": 1, "method": "GET", "url": "http://x/boot", "t": 0.0},
            {"index": 2, "method": "GET", "url": "http://x/order", "t": 0.34},
            {"index": 3, "method": "GET", "url": "http://x/other", "t": 0.353},
        ],
        bodies={
            "http://x/order": [json.dumps({"id": GUID_A, "label": "Order confirmed"})],
            "http://x/other": [json.dumps({"id": GUID_B})],
        },
        stamps=[{"node": "n1", "xhr": 3, "action": 0, "t": 0.36,
                 "text": f"Order {GUID_A} confirmed"}],
    )
    LiveBacking().on_load(doc, result)

    monkeypatch.delenv("WEBCLIENT_CORRELATOR", raising=False)
    assert "← after req[2, 3]" in doc.skeleton()  # default ordering: ambiguity preserved

    monkeypatch.setenv("WEBCLIENT_CORRELATOR", "content")
    sk = doc.skeleton()
    assert "← after req[2]" in sk and "req[2, 3]" not in sk  # content narrowed it


def test_content_falls_back_to_ordering_when_no_bodies_were_captured():
    # with no response bodies, ContentCorrelator is exactly the ordering baseline (same input,
    # same Correlation) -- the drop-in guarantee.
    net = [_net(1, "https://x/a", 0.0), _net(2, "https://x/b", 0.34), _net(3, "https://x/c", 0.353)]
    dom = [_mut("n1", 3, 0.36)]
    assert (
        ContentCorrelator(window_s=0.05).correlate(net, dom).candidates_for("n1")
        == OrderingCorrelator(window_s=0.05).correlate(net, dom).candidates_for("n1")
        == [2, 3]
    )
