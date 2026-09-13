"""WebSessionCore: a scoped ``WebClientCore``.

A session is the same engine core as the client, sharing its loop / http client
/ browser pool / event bus / plugins with a parent ``WebClientCore``, but with
its own identity (headers + cookies + name scope) and a ttl'd lifecycle. It
reuses the parent's whole fetch pipeline (``afetch``) -- it only injects its
identity into the request, absorbs Set-Cookie, and guards the lifecycle.
"""

from __future__ import annotations

import time
from typing import Any, Literal
from uuid import uuid4

from pydantic import PrivateAttr

from .client_core import WebClientCore
from .reference_core import ReferenceCore


class WebSessionCore(WebClientCore):
    """A ``WebClientCore`` scoped to one logical identity."""

    id: str = ""
    status: Literal["running", "expired", "closed"] = "running"
    session_headers: dict[str, str] = {}
    cookies: dict[str, str] = {}
    ttl: float | None = None
    expires_at: float | None = None

    _parent: Any = PrivateAttr(default=None)  # the owning engine core

    def model_post_init(self, ctx: Any) -> None:
        super().model_post_init(ctx)
        if not self.id:
            self.id = f"sess-{uuid4().hex[:8]}"
        if self.ttl is not None and self.expires_at is None:
            self.expires_at = time.time() + self.ttl

    def _init_transport(self) -> None:
        """A session shares the parent's pool (set in ``bind``)."""

    def bind(self, parent: WebClientCore) -> "WebSessionCore":
        """Share ``parent``'s engine (loop / http / browser / bus / plugins) and
        take a fresh name scope from it."""
        self._parent = parent
        self._render_table = parent._render_table  # plugins are shared
        self._scope = parent.new_scope()
        parent._sessions.append(self)
        return self

    # -- engine shared with the parent (loop / pool / bus / plugins) ---------
    def loop(self) -> Any:
        return self._parent.loop()

    @property
    def pool(self) -> Any:
        return self._parent.pool

    @property
    def bus(self) -> Any:
        return self._parent.bus

    # -- lifecycle -----------------------------------------------------------
    def _guard(self) -> None:
        if self.status == "closed":
            raise RuntimeError("session is closed")
        if self.expires_at is not None and time.time() > self.expires_at:
            self.status = "expired"
            raise RuntimeError("session has expired")

    def close(self) -> None:  # not the parent engine
        self.status = "closed"
        if self._scope is not None:
            self._scope.clear()

    # -- fetch: the parent's pipeline + this session's identity --------------
    async def afetch(
        self, ref: ReferenceCore, *, optional: bool = False, browser: bool = False
    ) -> Any:
        self._guard()
        scoped = ref.model_copy(
            update={
                "headers": {**self.session_headers, **ref.headers},
                "cookies": {**self.cookies, **ref.cookies},
            }
        )
        scoped._client = self
        scoped._session = self
        doc = await super().afetch(scoped, optional=optional, browser=browser)
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

    def document(self, name: str) -> Any:
        """Recover a document from THIS session's scope only."""
        from .document_core import DocumentCore

        obj = self._scope.get(name) if self._scope is not None else None
        return obj if isinstance(obj, DocumentCore) else None


__all__ = ["WebSessionCore"]
