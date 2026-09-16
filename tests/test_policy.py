"""Resolve policies (data) + the ``flags`` facet's access flags (their read-side):
a resolved document reads its own response into :class:`Flag`\\ s -- anti_bot_present
/ anti_bot_triggered / login_present / login_required."""

import pytest

from webclient import (
    AUTO,
    AntiBotPolicy,
    BrowserPolicy,
    ProxyPolicy,
    Resolve,
    RetryPolicy,
)
from webclient.core.document import Document
from webclient.core.reference.models import resolve_policy


def test_policy_defaults_and_auto_variants():
    assert BrowserPolicy().when == "never" and BrowserPolicy.auto().when == "auto"
    assert AntiBotPolicy().level == "off" and AntiBotPolicy.auto().level == "stealth"
    assert RetryPolicy().max == 2 and RetryPolicy.auto().max == 3
    # the bundle: proxy/antibot/browser off by default, all on under .auto()
    assert Resolve().proxy is None and Resolve().browser is None
    auto = Resolve.auto()
    assert auto.proxy is not None and auto.browser is not None
    assert auto.browser.when == "auto" and auto.antibot.level == "stealth"


def test_policies_are_frozen():
    with pytest.raises(Exception):
        RetryPolicy().max = 9  # type: ignore[misc]


def test_resolve_policy_normalises_the_kwarg():
    assert resolve_policy(None, BrowserPolicy) is None  # off / inherit
    assert resolve_policy("auto", BrowserPolicy).when == "auto"  # string
    assert resolve_policy(AUTO, ProxyPolicy).sticky == "session"  # sentinel
    p = BrowserPolicy(when="always")
    assert resolve_policy(p, BrowserPolicy) is p  # a policy passes through


def _doc(**kw):
    kw.setdefault("status_code", 200)
    return Document(url="http://x/", kind="html", **kw)


def test_flags_absent_on_a_normal_page():
    doc = _doc(content=b"<html><title>T</title><body>hi</body></html>")
    assert doc.flags() == []  # a plain page reports nothing present (total facet, empty)
    assert not doc.anti_bot_triggered() and not doc.login_required()


def test_anti_bot_triggered_flag_from_the_response():
    doc = _doc(status_code=403, response_headers={"x-datadome": "1"}, content=b"blocked")
    ab = doc.anti_bot_triggered()  # a named vendor on a blocking status
    assert ab.present and ab.value == "datadome" and ab.remedy == "stealth"
    assert ab.name in {f.name for f in doc.flags()}  # it shows in the digest
    # the vendor is present (a fingerprint), and here also triggered (a block)
    assert doc.anti_bot_present().present and doc.anti_bot_present().value == "datadome"


def test_bare_challenge_triggered_suggests_a_fresh_proxy_exit():
    doc = _doc(status_code=429, content=b"slow down")
    ab = doc.anti_bot_triggered()  # no named vendor -> a generic challenge
    assert ab.present and ab.remedy == "proxy"
    assert not doc.anti_bot_present().present  # no vendor fingerprint, just a status


def test_login_required_flag_has_no_transport_remedy():
    doc = _doc(status_code=401, content=b"unauthorized")
    lr = doc.login_required()
    assert lr.present and lr.remedy is None  # needs credentials, not an escalation
    assert not doc.anti_bot_triggered()  # a 401 is a login wall, not an anti-bot block
