"""Back-compat shim: ``WebClientCore`` and friends moved to the
:mod:`webclient.core.client` package. Re-exported here so existing imports
(``from webclient.core.client_core import WebClientCore``) keep working."""

from __future__ import annotations

from .client import (
    FetchBacking,
    NameScope,
    WebClientCore,
    _materialize,
    _retry_after_seconds,
)

__all__ = [
    "WebClientCore",
    "NameScope",
    "FetchBacking",
    "_materialize",
    "_retry_after_seconds",
]
