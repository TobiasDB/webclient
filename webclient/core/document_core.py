"""Back-compat shim: ``DocumentCore``/``Element`` and the html/json backings
moved to the :mod:`webclient.core.document` package. Re-exported here so existing
imports (``from webclient.core.document_core import ...``) keep working."""

from __future__ import annotations

from .document import DocumentCore, Element, HtmlBacking, JsonBacking

__all__ = ["DocumentCore", "Element", "HtmlBacking", "JsonBacking"]
