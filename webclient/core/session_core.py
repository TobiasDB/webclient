"""WebSessionCore: a session's core -- a shallow mimic of WebClientCore
(rewrite skeleton).

Same verbs (Fetch/Search/Crawl/Document), but scoped to one logical identity:
cookies/headers/storage persist across its fetches, and it delegates the engine
(loop/pool/bus) to its owning WebClientCore rather than owning one.
"""
from __future__ import annotations

from typing import Any, ClassVar, Literal

from pydantic import BaseModel, PrivateAttr

from .web_core import Backing, WebCore


class WebSessionCore(WebCore, BaseModel):
    """Core Fields (identity/state) + the same backings as the client, run
    against the owning client's engine with this session as the context."""

    # -- Core Fields (identity / lifecycle) ----------------------------------
    id: str = ""
    status: Literal["pending", "running", "expired", "closed"] = "pending"
    headers: dict[str, str] = {}
    cookies: dict[str, str] = {}
    ttl: float | None = None
    # TODO(port): expires_at / keep_alive / proxy / storage_state / timeout.

    _client: Any = PrivateAttr(default=None)     # owning WebClientCore (the engine)
    _scope: Any = PrivateAttr(default=None)      # this session's name scope

    # -- backings: same verbs as the client, session-scoped ------------------
    BACKINGS: ClassVar[tuple[Backing, ...]] = ()   # TODO: (Fetch, Search, Crawl, Document)

    # execute/remote_execute delegate to the owning WebClientCore with self as
    # the resolution context (TODO).


__all__ = ["WebSessionCore"]
