"""Back-compat shim: ``ReferenceCore`` and friends moved to the
:mod:`webclient.core.reference` package. Re-exported here so existing imports
(``from webclient.core.reference_core import ...``) keep working."""

from __future__ import annotations

from .reference import (
    DeriveBacking,
    HttpMethod,
    ReferenceCore,
    ResolveBacking,
    from_url,
)

__all__ = [
    "ReferenceCore",
    "DeriveBacking",
    "ResolveBacking",
    "HttpMethod",
    "from_url",
]
