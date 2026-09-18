"""The XHR->DOM correlation seam: the cheap OrderingCorrelator, from synthetic events.

No browser -- feed NetworkEvent/DOMUpdateEvent straight in and assert the Correlation.
This is the frozen interface a richer (content-matching) Correlator must also satisfy.
"""

from webclient.core.document.correlate import (
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


def _mut(node, xhr_index, t_s, kind="added"):
    return DOMUpdateEvent(
        kind=kind, node_id=node, detail={"xhr_index": xhr_index, "t_s": t_s},
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


def test_mutations_without_a_node_id_are_ignored():
    corr = OrderingCorrelator().correlate([_net(1, "https://x/a", 0.0)], [_mut("", 1, 0.1)])
    assert corr.phases == []  # no stable node -> no phantom phase


def test_skeleton_annotates_phase_and_never_shows_the_stamp():
    # the end-to-end wiring, no browser: a captured page (data-wc-node stamps in the HTML)
    # + a fake PageResult carrying the xhr timeline + phase stamps -> the skeleton lists the
    # requests and annotates the record region "← after [1]", but never leaks data-wc-node.
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
    assert "← after [1]" in sk  # the record region attributed to request 1
    assert "data-wc-node" not in sk  # the internal stamp is never shown (never a selector)
    assert "data-wc-node" not in doc.attr("html")  # nor leaked into rendered output


def test_models_round_trip():
    c = Correlation(
        requests=[XhrRequest(index=1, url="https://x/a", t_s=0.0)],
        phases=[DomPhase(node_key="n1", candidates=[1], t_s=0.1)],
    )
    assert Correlation.model_validate(c.model_dump()) == c
