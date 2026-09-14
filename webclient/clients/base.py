"""``Client`` + ``ClientFactory``: the leasable transport abstractions.

A ``Client`` is one unit of transport concurrency (one httpx client, or one
browser page) and owns the actual transport work for its medium. Factories know
how to build (and tear down) each kind; the ``ClientPool`` bounds and recycles
them. Nothing above the pool touches httpx or playwright directly.
"""

from __future__ import annotations

import abc


class Client(abc.ABC):
    """A leased transport resource -- the functionality for one medium."""

    kind: str

    async def reset(self) -> None:
        """Clean per-lease state before the client is reused (default: none)."""

    @abc.abstractmethod
    async def aclose(self) -> None: ...


class ClientFactory(abc.ABC):
    """Builds one kind of ``Client`` and owns that kind's shared resources."""

    kind: str

    @abc.abstractmethod
    async def create(self) -> Client: ...

    async def aclose(self) -> None:
        """Tear down factory-level resources (default: none)."""


__all__ = ["Client", "ClientFactory"]
