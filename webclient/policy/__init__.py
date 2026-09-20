"""Resolve policy -- the clean space for how a reference is fetched.

The policy models (retry / rate / proxy / anti-bot / browser, the ``BrowserConfig`` launch
settings, and the ``Resolve`` bundle) live in :mod:`.models`; :mod:`.headers` turns a
``Resolve`` into the ``X-WebClient-*`` request headers a proxy service reads. The client
just USES these (a ``WebClient`` field default, per-call overridable) -- ``from
webclient.policy import Resolve, BrowserConfig, AUTO``.
"""

from __future__ import annotations

from .headers import policy_headers
from .models import (
    AUTO,
    AntiBotPolicy,
    BrowserConfig,
    BrowserPolicy,
    ProxyPolicy,
    RatePolicy,
    Resolve,
    RetryPolicy,
    resolve_policy,
)

__all__ = [
    "RetryPolicy", "RatePolicy", "ProxyPolicy", "AntiBotPolicy", "BrowserPolicy",
    "BrowserConfig", "Resolve", "AUTO", "resolve_policy", "policy_headers",
]
