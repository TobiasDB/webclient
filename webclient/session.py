"""Session: logical identity spanning many fetches (M3, http side).

Cookies and headers persist across fetches made with the session; responses
write cookies back. ``ttl``/``keep_alive``/``status`` follow the
Browserbase-shaped lifecycle from the spec; browser storage_state is used
from M4.
"""
from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any, Literal, cast, overload

from pydantic import BaseModel, Field, PrivateAttr

from .core.webclient import Proxy

if TYPE_CHECKING:
    from .stubs import Lazy, LazyDocument, LazyReference
from .document import Document, HttpMethod, Reference


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
    _scope: Any = PrivateAttr(default=None)    # NameScope: this session's names

    def ref(self, url: str, method: HttpMethod = "get",
            **kwargs: Any) -> "LazyReference":
        """A LAZY reference root scoped to this session: records ops and runs on
        ``.collect()`` (or ``session.execute``), resolving within this session
        (PLAN §8 -- was eager). The plan carries this session's id."""
        from .core.expr import Expr, Plan
        spec = Reference.from_url(url, method=method, **kwargs).request_fields()
        return cast("LazyReference", Expr(
            Plan(root="Reference", source=spec, session_id=self.id), self._client))

    def document(self, name: str) -> Document | None:
        found = self._scope.get(name) if self._scope is not None else None
        return found if isinstance(found, Document) else None

    def reference(self, name: str) -> Reference | None:
        found = self._scope.get(name) if self._scope is not None else None
        return found if isinstance(found, Reference) and not isinstance(
            found, Document) else None

    # -- client surface bound to this session (WebSession, PLAN §5d) ---------
    # Same plan builders as the WebClient facade, run on the core with this
    # session as the resolution context (see webclient.client).
    def fetch(self, ref: "Reference | str", *, browser: bool = False,
              optional: bool = False, **options: Any) -> "LazyDocument":
        """Lazy resolve within this session: a Document expr (session-scoped);
        run with ``.collect()`` / ``session.execute`` (PLAN §8 -- was eager)."""
        from .core.base import RAISE, RETURN
        from .core.expr import Expr, Plan
        r = ref if isinstance(ref, Reference) else Reference.from_url(ref)
        root = Expr(Plan(root="Reference", source=r.request_fields(),
                         session_id=self.id), self._client)
        return cast("LazyDocument", root.resolve(
            browser=browser, optional=optional,
            error=RETURN if optional else RAISE, **options))

    @overload
    def execute[T](self, expr: "Lazy[T]", context: Any = ...) -> T: ...
    @overload
    def execute(self, expr: Any, context: Any = ..., *, stream: bool = ...) -> Any: ...
    def execute(self, expr: Any, context: Any = None, *,
                stream: bool = False) -> Any:
        """Run a lazy expression within this session (sync bridge); a lazy tier
        materialises to its model via the ``Lazy[T]`` bridge."""
        from .client import run_on_core
        return run_on_core(self._client, expr, context, stream=stream)

    def search(self, term: str, *, engine: Any = None,
               limit: int = 5) -> list[dict[str, Any]]:
        """Run a search within this session and return result rows."""
        from urllib.parse import quote_plus
        from .client import default_engine, search_expr
        eng = engine or default_engine()
        rows = self.execute(search_expr(eng, limit=limit),
                            self.ref(eng.url.format(q=quote_plus(term))))
        return cast("list[dict[str, Any]]", rows)

    def check(self) -> None:
        """Raise unless the session is usable; lazily expires on ttl."""
        if self.expires_at is not None and time.time() > self.expires_at:
            if self.status == "running":
                self.status = "expired"
        if self.status in ("expired", "closed"):
            raise RuntimeError(f"session {self.id} is {self.status}")

    def close(self) -> None:
        if self.status == "closed":
            return
        if self._client is not None:
            try:
                self._client._teardown_session(self)
            except RuntimeError:
                pass                       # engine already stopped
        self.status = "closed"
