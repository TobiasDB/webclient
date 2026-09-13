"""WebSessionCore: a session's core -- a shallow mimic of WebClientCore
(rewrite skeleton).

Same verbs (Fetch/Search/Crawl/Document), but scoped to one logical identity:
cookies/headers/storage persist across its fetches, and it delegates the engine
(loop/pool/bus) to its owning WebClientCore rather than owning one.
"""
from __future__ import annotations

from typing import Any, ClassVar, Literal

from pydantic import BaseModel, PrivateAttr

from .reference_core import ReferenceCore
from .web_core import Backing, WebCore


class WebSessionCore(WebCore, BaseModel):
    """Core Fields (identity/state) + the same fetch path as the client, run
    against the owning client's engine with this session's identity applied.
    Cookies/headers persist across its fetches; sessions do not share cookies."""

    # -- Core Fields (identity / lifecycle) ----------------------------------
    id: str = ""
    status: Literal["pending", "active", "expired", "closed"] = "pending"
    headers: dict[str, str] = {}
    cookies: dict[str, str] = {}
    ttl: float | None = None

    _client: Any = PrivateAttr(default=None)     # owning WebClientCore (the engine)
    _surface: Any = PrivateAttr(default=None)

    BACKINGS: ClassVar[tuple[Backing, ...]] = ()

    # -- session-scoped fetch ------------------------------------------------
    def fetch(self, ref: ReferenceCore, *, optional: bool = False) -> Any:
        """Fetch through the owning client with this session's headers/cookies
        applied, then absorb any Set-Cookie back into the session."""
        scoped = ref.model_copy(update={
            "headers": {**self.headers, **ref.headers},
            "cookies": {**self.cookies, **ref.cookies}})
        scoped._client = self._client
        scoped._session = self
        doc = self._client.fetch(scoped, optional=optional)
        self._absorb(doc)
        self.status = "active"
        return doc

    def _absorb(self, doc: Any) -> None:
        raw = doc.response_headers.get("set-cookie", "")
        for chunk in raw.split(", "):
            pair = chunk.split(";")[0].strip()
            if "=" in pair:
                key, value = pair.split("=", 1)
                self.cookies[key.strip()] = value.strip()


__all__ = ["WebSessionCore"]
