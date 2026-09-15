"""Resolve policies (data) + the ``probe`` facet op (their read-side). P0:
the models exist and the facet projects a recorded ProbeRecord; the escalation
ladder that would write richer records lands in later phases.
"""

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
from webclient.core.document.models import ProbeRecord
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
    return Document(url="http://x/", kind="html", status_code=200, **kw)


def test_probe_facet_absent_without_a_record():
    doc = _doc(content=b"<html><title>T</title></html>")
    assert not doc.has_op("probe")  # a plain fetch records nothing to probe


def test_probe_facet_projects_the_record():
    doc = _doc(content=b"<html></html>")
    doc._probe = ProbeRecord(was_browser_required=True, final_tier="browser")
    probe = doc.probe()
    assert probe is not None and probe.was_browser_required is True
    assert probe.paywall is None  # lean: false flags project to None


def test_probe_facet_reports_anti_bot():
    doc = _doc(content=b"<html></html>")
    doc._probe = ProbeRecord(anti_bot="cloudflare", was_proxy_required=True)
    probe = doc.probe()  # the probe facet is its own op
    assert probe is not None
    assert probe.anti_bot == "cloudflare" and probe.was_proxy_required is True
