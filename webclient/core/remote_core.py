"""Back-compat shim: ``RemoteWebClientCore``/``RemoteSession`` moved to the
:mod:`webclient.core.remote` package. Re-exported here so existing imports
(``from webclient.core.remote_core import RemoteWebClientCore``) keep working."""

from __future__ import annotations

from .remote import RemoteSession, RemoteWebClientCore

__all__ = ["RemoteWebClientCore", "RemoteSession"]
