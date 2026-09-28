"""The fetch layer's interface: a :class:`Fetcher` turns a :class:`Request` into a
:class:`Snapshot`, and never raises for a transport failure (it records it on
``snapshot.error``). Every transport -- static HTTP, a browser, a HAR replay -- implements
this one method, so a caller (or the resolve layer) depends on the interface, not on httpx or
playwright. Fetchers are async-native; a sync/lazy/remote face is the DSL layer's job.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from .request import Request
from .snapshot import Snapshot


@runtime_checkable
class Fetcher(Protocol):
    """Performs one request and returns its Snapshot. Implementations own a transport resource
    and should be closed with :meth:`aclose` when done."""

    async def fetch(self, request: Request) -> Snapshot: ...

    async def aclose(self) -> None: ...


__all__ = ["Fetcher"]
