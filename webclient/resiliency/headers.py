"""Translate a :class:`~webclient.core.reference.models.Resolve` bundle into the
request headers a proxy / unblocker service reads.

Rate-limit, retry and proxy behaviour that a downstream proxy service performs is
declared to it per-request as ``X-WebClient-*`` headers (the service is assumed to
exist and to honour them). This keeps the client a thin declarer: it states the
policy; the service enforces the network-level parts (a rotating proxy pool, a
server-side rate gate, upstream retries). Local politeness/retry still apply on
top -- these headers are the *delegated* form, not a replacement.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Iterable

if TYPE_CHECKING:
    from ..core.reference.models import (
        ProxyPolicy,
        RatePolicy,
        Resolve,
        RetryPolicy,
    )


def _csv(values: "Iterable[object]") -> str:
    """A frozenset/list of scalars as a stable, comma-joined header value."""
    return ",".join(str(v) for v in sorted(values, key=str))


def _retry_headers(retry: "RetryPolicy") -> dict[str, str]:
    return {
        "X-WebClient-Retry-Max": str(retry.max),
        "X-WebClient-Retry-Backoff": retry.backoff,
        "X-WebClient-Retry-Base": str(retry.base),
        "X-WebClient-Retry-On": _csv(retry.on_statuses),
    }


def _rate_headers(rate: "RatePolicy") -> dict[str, str]:
    h: dict[str, str] = {"X-WebClient-Rate-Per": rate.per}
    if rate.rps is not None:
        h["X-WebClient-Rate-Rps"] = str(rate.rps)
    if rate.concurrency is not None:
        h["X-WebClient-Rate-Concurrency"] = str(rate.concurrency)
    if rate.burst != 1:
        h["X-WebClient-Rate-Burst"] = str(rate.burst)
    if rate.adaptive:
        h["X-WebClient-Rate-Adaptive"] = "1"
    return h


def _proxy_headers(proxy: "ProxyPolicy") -> dict[str, str]:
    h: dict[str, str] = {"X-WebClient-Proxy": "on", "X-WebClient-Proxy-Sticky": proxy.sticky}
    if proxy.pool is not None:
        h["X-WebClient-Proxy-Pool"] = proxy.pool if isinstance(proxy.pool, str) else _csv(proxy.pool)
    if proxy.geo is not None:
        h["X-WebClient-Proxy-Geo"] = proxy.geo
    if proxy.rotate_on:
        h["X-WebClient-Proxy-Rotate-On"] = _csv(proxy.rotate_on)
    return h


def policy_headers(resolve: "Resolve | None") -> dict[str, str]:
    """The ``X-WebClient-*`` headers that declare ``resolve``'s rate/retry/proxy
    policy to a proxy service. ``None`` (or a resolve with only defaults and no
    proxy) yields the retry/rate declaration; the proxy block appears only when a
    :class:`ProxyPolicy` is configured. Empty dict for ``None``."""
    if resolve is None:
        return {}
    headers = {**_retry_headers(resolve.retry), **_rate_headers(resolve.rate)}
    if resolve.proxy is not None:
        headers.update(_proxy_headers(resolve.proxy))
    return headers


__all__ = ["policy_headers"]
