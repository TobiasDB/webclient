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

from typing import TYPE_CHECKING, Any, cast

from .web_core import WebCore

if TYPE_CHECKING:
    from .engine import Engine


class SessionCore(WebCore):
    """A scope on a shared :class:`~.engine.Engine` (see the module docstring)."""

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


__all__ = ["SessionCore"]
