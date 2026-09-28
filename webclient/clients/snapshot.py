"""The Snapshot: the fetch -> parse seam.

A transport client performs one request and interprets the response into this pure data --
the requested and final URL, the sniffed content kind, the raw bytes, the status, response
headers, encoding, the Set-Cookie jar, timing, and any transport error. That is the whole
output of the fetch layer: no ``httpx``/``playwright`` type crosses it. The parse layer
turns a Snapshot into a ``Document`` (``webclient.core.document.build_document``), so the
transport never needs to know what a Document is.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from ..kernel.errors import WebError


class Snapshot(BaseModel):
    """The raw result of a transport fetch (see the module docstring). Defaults describe a
    transport failure before any response (``status_code == 0`` with an ``error``)."""

    url: str = ""  # the requested URL
    final_url: str | None = None  # after redirects (None on a transport failure)
    kind: Literal["html", "json", "xml", "binary"] = "html"  # sniffed content kind
    content: bytes = b""
    status_code: int = 0  # 0 = no response (transport failure)
    response_headers: dict[str, str] = {}
    encoding: str | None = None
    elapsed: float | None = None
    #: the Set-Cookie jar aggregated across the redirect history + final hop (transport-parsed,
    #: so an ``Expires`` comma is handled) -- a session absorbs these into its own cookies.
    set_cookies: dict[str, str] = {}
    error: WebError | None = None


__all__ = ["Snapshot"]
