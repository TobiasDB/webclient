"""Transport clients + the pool that leases them.

The one place that touches httpx / playwright. A ``Client`` (``HTTPXClient`` /
``BrowserClient``) owns the transport work for its medium; a ``ClientFactory``
builds it; the ``ClientPool`` bounds and recycles leases. ``WebClient`` holds
a pool and leases from it; nothing above the pool sees a raw transport.
"""

from __future__ import annotations

from .base import Client, ClientFactory
from .browser import BrowserClient, BrowserFactory, PageResult, PageScript
from .http import HTTPXClient, HTTPXFactory, charset_of, sniff_kind
from .pool import ClientPool, Lease, PoolStats

__all__ = [
    "Client",
    "ClientFactory",
    "HTTPXClient",
    "HTTPXFactory",
    "BrowserClient",
    "BrowserFactory",
    "PageScript",
    "PageResult",
    "ClientPool",
    "Lease",
    "PoolStats",
    "sniff_kind",
    "charset_of",
]
