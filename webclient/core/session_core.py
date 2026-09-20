"""SessionCore: a scope on a shared Engine.

Every stateful, context-managed core -- the root :class:`~.client.WebClient`, a
``Session``, a ``Crawl`` (and later a ``Record``) -- is a ``SessionCore``: it borrows a
shared :class:`~.engine.Engine` (it never owns its own) and adds its own STORE
(py-memory for now: a crawl's frontier/seen, a recorder's plan). Sessions NEST -- a
session opened on a session shares the one engine and chains its parent -- so a
``WebClient`` session, a ``Record`` and a ``Crawl`` can stack without spawning
independent engines. State lives on the session; every op reaches the engine through
dispatch, so a session is sync / async / lazy / remote uniformly.

Step 1c of the session-base refactor: this only names the shared "scope on an engine"
concept and hangs the engine-resolution + store on it. The concrete cores keep their
own fields (scope / frontier / cookies) and ops; ``WebClient`` and ``Crawl`` now extend
this base. No behaviour change.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any, Literal, cast

from pydantic import BaseModel

from .web_core import WebCore

if TYPE_CHECKING:
    from .engine import Engine


class ISession(BaseModel):
    """The shared session-lifecycle FIELDS -- the ``I<Core>`` interface a session inherits
    (like ``IWebClient`` / ``ICrawl`` hold their core's fields). Declared ONCE here, so
    every session kind carries the same ``id`` / ``status`` / ``ttl`` / ``expires_at``.
    ``id`` is "" on the root client (an identity session names itself); ``ttl`` /
    ``expires_at`` bound its lifetime (``None`` = unbounded)."""

    id: str = ""
    status: Literal["running", "expired", "closed"] = "running"
    ttl: float | None = None
    expires_at: float | None = None


class SessionCore(WebCore, ISession):
    """A scope on a shared :class:`~.engine.Engine` with ONE session LIFECYCLE (see the
    module docstring). Every session -- the root :class:`~.client.WebClient`, a ``Session``,
    a ``Crawl``, a ``Record`` -- carries the shared :class:`ISession` fields and the same
    ``close`` / guard here, so the lifecycle is declared ONCE. What differs is each
    session's own DATA (an identity session's cookies, a crawl's frontier/pages, a
    recorder's plan) and behaviour (its backing) -- the implementations own their typing;
    :class:`SessionCore` only offers the storage interface + lifecycle.

    (``_parent`` -- the owning session a child borrows from -- is a PrivateAttr the concrete
    cores declare; the methods below read it defensively with ``getattr``, so the root
    client, which has none, resolves to itself.)"""

    def _the_engine(self) -> "Engine":
        """The engine this session is bound to: a child session borrows its parent's
        (``_parent``), a crawl its client's (``_client``), the root client owns its own
        (``_engine``). The single resolver every session-side op reaches the engine by."""
        owner = getattr(self, "_parent", None) or getattr(self, "_client", None) or self
        return cast("Engine", cast(Any, owner)._engine)  # _engine lives on the concrete core

    @property
    def store(self) -> dict[str, Any]:
        """This session's own state (py-memory for now): empty on the root client; a
        crawl keeps its frontier/seen here, a recorder its plan. Scoped to this session
        -- nested sessions each have their own, all sharing the one engine."""
        return cast("dict[str, Any]", cast(Any, self)._store)  # _store lives on the concrete core

    # -- lifecycle (shared by every session kind) ----------------------------
    def _arm_ttl(self) -> None:
        """Start the ttl clock: set ``expires_at`` from ``ttl`` (called at construction by a
        session that takes a ttl). A no-op when unbounded or already armed."""
        if self.ttl is not None and self.expires_at is None:
            self.expires_at = time.time() + self.ttl

    def _guard(self) -> None:
        """Refuse to act on a closed or past-ttl session (marking it ``expired`` on the way)."""
        if self.status == "closed":
            raise RuntimeError("session is closed")
        if self.expires_at is not None and time.time() > self.expires_at:
            self.status = "expired"
            raise RuntimeError("session has expired")

    def close(self) -> None:
        """Close THIS session (not the shared engine): mark it closed, dispose any server-side
        session it holds, and release its name scope. The root :class:`WebClient` overrides
        this to also tear down the engine it owns."""
        if self.status == "closed":
            return
        self.status = "closed"
        sid = getattr(self, "_server_sid", "")
        if sid:  # a remote session -- dispose its server session over the shared transport
            self._the_engine().close_server_session(sid)
        scope = getattr(self, "_scope", None)
        if scope is not None:
            scope.clear()


__all__ = ["SessionCore"]
