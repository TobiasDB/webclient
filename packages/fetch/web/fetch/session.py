"""``Session`` -- a stateful handle a backend opens, which OWNS its resources.

A session is the stateful form of a backend: it owns what persists across fetches -- an HTTP
cookie jar, or a browser context + page -- and closing it releases them. This is a key lesson
from the old client: **page ownership lives on the session**. A live browser page is never
floated back to a caller to close by hand; it belongs to a session, and the session's lifecycle
is the page's. Nothing else opens or closes it.

A backend's one-shot ``fetch(request)`` is simply a session opened and closed for a single
request. A workflow that needs state (cookies across requests, or clicking through one page)
opens a session, drives it, and closes it -- and the session owns everything in between.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from .request import Request
from .snapshot import Snapshot


@runtime_checkable
class Session(Protocol):
    """A stateful, resource-owning fetch handle. ``fetch`` performs a request within the session
    (state persists); ``aclose`` releases everything the session owns. A browser session adds
    state-mutating actions (``click`` / ``type`` / ``wait_for``) and ``snapshot`` on top."""

    async def fetch(self, request: Request) -> Snapshot: ...

    async def aclose(self) -> None: ...


__all__ = ["Session"]
