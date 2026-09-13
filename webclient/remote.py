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
        resp.raise_for_status()
        return self._deserialize(resp.json()["rows"])

    def _deserialize(self, rows: Any) -> Any:
        if isinstance(rows, dict) and "__doc__" in rows:
            return _RemoteDoc(rows["__doc__"], self)
        if isinstance(rows, list):
            return [self._deserialize(r) for r in rows]
        return rows

    def close(self) -> None:
        self._http.close()


class RemoteWebClient:
    """The remote client: same ref/fetch/execute surface as ``WebClient``, run
    server-side."""

    def __init__(self, url: str, token: str | None = None) -> None:
        self._core = RemoteWebClientCore(url, token)

    def ref(self, url: str, method: str = "get", **kw: Any) -> Any:
        spec = _core_from_url(url, method, **kw).model_dump()
        return Expr(Plan(root="Reference", source=spec), self._core)

    lazy = ref

    def fetch(self, url: str, **kw: Any) -> Any:
        return self.ref(url, **kw).resolve()

    def execute(self, expr: Any, context: Any = None) -> Any:
        return self._core.remote_execute(expr, context)

    def close(self) -> None:
        self._core.close()

    def __enter__(self) -> "RemoteWebClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self._core.close()


__all__ = ["RemoteWebClient", "RemoteWebClientCore"]
