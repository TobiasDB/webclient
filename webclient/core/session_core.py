"""Back-compat shim: ``WebSessionCore`` moved to the
:mod:`webclient.core.session` package. Re-exported here so existing imports
(``from webclient.core.session_core import WebSessionCore``) keep working."""

from __future__ import annotations

from .session import WebSessionCore

__all__ = ["WebSessionCore"]
