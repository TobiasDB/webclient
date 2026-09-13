"""WebSessionCore: a logical identity (cookies/headers/ttl) spanning fetches.

A session runs against the owning client's engine but applies its own identity:
cookies/headers persist across its fetches and do not leak between sessions. It
has a ttl'd lifecycle (running -> expired / closed) and tags the documents and
events it produces with its id.
"""

from __future__ import annotations

import time
from typing import Any, ClassVar, Literal
from uuid import uuid4

from pydantic import BaseModel, PrivateAttr

from .reference_core import ReferenceCore
from .web_core import Backing, WebCore


class WebSessionCore(WebCore, BaseModel):
    """Core Fields (identity/lifecycle) + a session-scoped fetch."""

    id: str = ""
    status: Literal["running", "expired", "closed"] = "running"
    headers: dict[str, str] = {}
    cookies: dict[str, str] = {}
    ttl: float | None = None
    expires_at: float | None = None

    _client: Any = PrivateAttr(default=None)  # owning WebClientCore (the engine)
    _scope: Any = PrivateAttr(default=None)  # this session's NameScope
    _surface: Any = PrivateAttr(default=None)

    BACKINGS: ClassVar[tuple[Backing, ...]] = ()

    def model_post_init(self, _ctx: Any) -> None:
        if not self.id:
            self.id = f"sess-{uuid4().hex[:8]}"
        if self.ttl is not None and self.expires_at is None:
            self.expires_at = time.time() + self.ttl

    # -- lifecycle -----------------------------------------------------------
    def _guard(self) -> None:
        if self.status == "closed":
            raise RuntimeError("session is closed")
        if self.expires_at is not None and time.time() > self.expires_at:
            self.status = "expired"
            raise RuntimeError("session has expired")

    def close(self) -> None:
        self.status = "closed"
        if self._scope is not None:  # retention ends
            self._scope.clear()

    def document(self, name: str) -> Any:
        """Recover a document from THIS session's scope only."""
        from .document_core import DocumentCore

        obj = self._scope.get(name) if self._scope is not None else None
        return obj if isinstance(obj, DocumentCore) else None

    # -- session-scoped fetch ------------------------------------------------
    def fetch(
        self, ref: ReferenceCore, *, optional: bool = False, browser: bool = False
    ) -> Any:
        self._guard()
        scoped = ref.model_copy(
            update={
                "headers": {**self.headers, **ref.headers},
                "cookies": {**self.cookies, **ref.cookies},
            }
        )
        scoped._client = self._client
        scoped._session = self
        doc = self._client.fetch(scoped, optional=optional, browser=browser)
        doc.session_id = self.id
        for event in doc._events:
            event.session_id = self.id
        self._absorb(doc)
        return doc

    def _absorb(self, doc: Any) -> None:
        raw = doc.response_headers.get("set-cookie", "")
        for chunk in raw.split(", "):
            pair = chunk.split(";")[0].strip()
            if "=" in pair:
                key, value = pair.split("=", 1)
                self.cookies[key.strip()] = value.strip()


__all__ = ["WebSessionCore"]
