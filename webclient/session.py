"""Session: identity that spans many resolves — cookies, headers, proxy and
browser storage."""
from __future__ import annotations

import time
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr

from .errors import WebClientError
from .reference import HttpMethod, Proxy, Reference

Status = Literal["pending", "running", "expired", "closed"]


class Session(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    id: str = Field(default_factory=lambda: uuid4().hex)
    status: Status = "running"
    ttl: float | None = None
    keep_alive: bool = False
    expires_at: float | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    cookies: dict[str, str] = Field(default_factory=dict)
    proxy: Proxy | None = None
    storage_state: dict[str, Any] | None = None
    timeout: float | None = None

    _client: Any = PrivateAttr(default=None)

    def model_post_init(self, _: Any) -> None:
        if self.ttl is not None and self.expires_at is None:
            self.expires_at = time.time() + self.ttl

    def ref(self, url: str, method: HttpMethod = "get",
            **fields: Any) -> Reference:
        """A Reference bound to this session and its client."""
        return Reference.from_url(url, method=method, **fields).bind(
            self._client, self)

    def resolve(self, target: "str | Reference", **options: Any) -> Any:
        return self._client.resolve(target, session=self, **options)

    def check(self) -> None:
        """Raise unless usable; expires lazily on ttl."""
        if (self.expires_at is not None and time.time() > self.expires_at
                and self.status == "running"):
            self.status = "expired"
        if self.status in ("expired", "closed"):
            raise WebClientError(f"session {self.id} is {self.status}")

    @property
    def alive(self) -> bool:
        try:
            self.check()
        except WebClientError:
            return False
        return True

    def close(self) -> None:
        if self.status == "closed":
            return
        if self._client is not None:
            self._client._close_session(self)
        self.status = "closed"

    def __repr__(self) -> str:
        return (f"Session({self.id[:8]}, {self.status}, "
                f"cookies={len(self.cookies)})")

    __str__ = __repr__
