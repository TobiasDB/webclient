"""Remote backend: the same lazy interface, executed server-side over HTTP.

``RemoteWebClient`` builds the very same plans as ``WebClient`` but runs them on
a ``webclient.service`` app -- so there is no local browser or lxml, only httpx
+ pydantic. A fetched document comes back as a shallow lazy handle
(``_RemoteDoc``): its metadata (title/ok/kind) is inline, and any op on it is a
plan rooted at the server-side document id, run with one more round trip.
"""
from __future__ import annotations

from typing import Any

import httpx

from .core.reference_core import ReferenceCore
from .core.reference_core import from_url as _core_from_url
from .expr import Expr
from .plan import Plan
from .surfaces import WebClient


def _url_of(source: dict[str, Any]) -> str:
    return ReferenceCore(**source).dispatch("url")


class _RemoteDoc:
    """A server-side document handle: metadata inline, ops as remote plans."""

    def __init__(self, meta: dict[str, Any], core: "RemoteWebClientCore") -> None:
        object.__setattr__(self, "_meta", meta)
        object.__setattr__(self, "_core", core)

    def __getattr__(self, name: str) -> Any:
        meta = object.__getattribute__(self, "_meta")
        if name in meta:                            # title / ok / kind / id
            return meta[name]
        core = object.__getattribute__(self, "_core")
        root = Expr(Plan(root="Document", source={"document_id": meta["id"]}), core)
        return getattr(root, name)

    def __repr__(self) -> str:
        return f"_RemoteDoc({object.__getattribute__(self, '_meta')})"


class RemoteWebClientCore:
    """A client core whose ``remote_execute`` POSTs a plan to ``/execute``."""

    def __init__(self, url: str, token: str | None = None) -> None:
        self.url = url.rstrip("/")
        self.token = token
        self._http = httpx.Client()

    def remote_execute(self, expr: Expr, context: Any = None) -> Any:
        body: dict[str, Any] = {"plan": expr._plan.model_dump()}
        src = expr._plan.source
        if src and "document_id" in src:
            body["document_id"] = src["document_id"]
        elif src:                                   # a reference-rooted plan
            body["url"] = _url_of(src)
        elif isinstance(context, _RemoteDoc):
            body["document_id"] = context._meta["id"]
        elif isinstance(context, Expr) and context._plan.source:
            body["url"] = _url_of(context._plan.source)
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        resp = self._http.post(f"{self.url}/execute", json=body, headers=headers)
        if not (200 <= resp.status_code < 300):
            from .errors import RemoteError
            raise RemoteError(resp.status_code, resp.text[:200])
        return self._deserialize(resp.json()["rows"])

    def _deserialize(self, rows: Any) -> Any:
        if isinstance(rows, dict) and "__doc__" in rows:
            return _RemoteDoc(rows["__doc__"], self)
        if isinstance(rows, list):
            return [self._deserialize(r) for r in rows]
        return rows

    def close(self) -> None:
        self._http.close()

    # -- server-side sessions ------------------------------------------------
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    def create_session(self, ttl: float | None = None) -> dict[str, Any]:
        resp = self._http.post(f"{self.url}/sessions", json={"ttl": ttl},
                               headers=self._headers())
        resp.raise_for_status()
        return resp.json()

    def close_session(self, sid: str) -> None:
        self._http.delete(f"{self.url}/sessions/{sid}", headers=self._headers())


class RemoteSession:
    """A handle to a server-side session; its fetches thread the session id
    into the plan so the server resolves them through that session."""

    def __init__(self, core: RemoteWebClientCore, sid: str) -> None:
        self._core = core
        self._id = sid
        self._status = "running"

    def ref(self, url: str, method: str = "get", **kw: Any) -> Any:
        spec = _core_from_url(url, method, **kw).model_dump()
        return Expr(Plan(root="Reference", source=spec, session_id=self._id),
                    self._core)

    def fetch(self, url: str, **kw: Any) -> Any:
        return self.ref(url, **kw).resolve()

    def close(self) -> None:
        self._core.close_session(self._id)
        self._status = "closed"

    @property
    def id(self) -> str:
        return self._id

    @property
    def status(self) -> str:
        return self._status


class RemoteWebClient(WebClient):
    """The remote client is literally a ``WebClient`` over a remote core: the
    same ref/fetch/execute plan-building surface, executed server-side."""

    def __init__(self, url: str, token: str | None = None) -> None:
        super().__init__(core=RemoteWebClientCore(url, token))

    def session(self, *, ttl: float | None = None, **kw: Any) -> RemoteSession:
        info = self._core.create_session(ttl)
        return RemoteSession(self._core, info["id"])


__all__ = ["RemoteWebClient", "RemoteWebClientCore"]
