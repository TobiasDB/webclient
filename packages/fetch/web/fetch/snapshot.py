"""``Snapshot`` -- the output of the fetch layer, and its whole contract with parse.

A Snapshot is *request + response bytes + events*: what was asked, the raw bytes that came
back with their transport metadata (final URL, status, headers, timing, Set-Cookie), and any
events captured during the fetch (a browser fetch carries DOM/network/console events; a static
one carries none). It is pure data -- no ``httpx``/``playwright`` type crosses it.

Deliberately, a Snapshot does NOT sniff the content kind or decode a charset: that is the
parse layer's job (``bytes -> Document``). Fetch reports transport facts; parse interprets
them. ``error`` is set only on a TRANSPORT failure (no response at all) -- a 404 is a valid
Snapshot with ``status == 404`` and ``error is None``; whether a status counts as a failure is
a policy decision for the resolve layer, not the transport.
"""

from __future__ import annotations

from pydantic import BaseModel

from web.kernel import Event, WebError

from .request import Request


class Snapshot(BaseModel):
    """The raw result of performing a :class:`Request` (see the module docstring)."""

    request: Request
    url: str = ""  # the final URL after redirects (== request.url when there were none)
    status: int = 0  # 0 = no response (transport failure)
    headers: dict[str, str] = {}
    content: bytes = b""
    elapsed: float = 0.0
    set_cookies: dict[str, str] = {}
    redirects: list[str] = []  # the intermediate URLs, in order
    events: list[Event] = []  # captured during the fetch (browser: DOM/network/console)
    error: WebError | None = None

    @property
    def ok(self) -> bool:
        """A response arrived with a 2xx status (a 404 is a valid Snapshot but not ``ok``)."""
        return self.error is None and 200 <= self.status < 300


__all__ = ["Snapshot"]
