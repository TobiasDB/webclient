"""Session: logical identity spanning many fetches (M3, http side).

Cookies and headers persist across fetches made with the session; responses
write cookies back. ``ttl``/``keep_alive``/``status`` follow the
Browserbase-shaped lifecycle from the spec; browser storage_state is used
from M4.
"""
from __future__ import annotations

import time
from typing import Any, Literal

from pydantic import BaseModel, Field, PrivateAttr

from .models import HttpMethod, Proxy, Reference


class Session(BaseModel):
    id: str = ""
    status: Literal["pending", "running", "expired", "closed"] = "pending"
    ttl: float | None = None
    keep_alive: bool = False
    expires_at: float | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    cookies: dict[str, str] = Field(default_factory=dict)
    proxy: Proxy | None = None
    storage_state: dict[str, Any] | None = None
    timeout: float | None = None

    _client: Any = PrivateAttr(default=None)   # owning WebClient

    def ref(self, url: str, method: HttpMethod = "get",
            **kwargs: Any) -> Reference:
        """A Reference bound to this session (and its WebClient)."""
        return Reference.from_url(url, method=method, **kwargs).bind(
            self._client, self)

    def check(self) -> None:
        """Raise unless the session is usable; lazily expires on ttl."""
        if self.expires_at is not None and time.time() > self.expires_at:
            if self.status == "running":
                self.status = "expired"
        if self.status in ("expired", "closed"):
            raise RuntimeError(f"session {self.id} is {self.status}")

    def close(self) -> None:
        if self.status != "closed":
            self.status = "closed"
