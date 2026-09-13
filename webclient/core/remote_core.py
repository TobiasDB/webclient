"""Remote backend: a ``WebClientCore`` that executes over HTTP.

Remote is not a separate client -- it is the same surface over a swapped core.
``RemoteWebClientCore`` is a ``WebClientCore`` whose ``execute`` POSTs the plan
to a ``webclient.service`` app instead of running it on a local engine, so a
``WebClient`` over it builds the very same plans (the inherited fetch backing)
with no local browser or lxml -- only httpx + pydantic. A fetched document comes
back as a shallow handle (``_RemoteDoc``): metadata (title/ok/kind) inline, any
op a plan rooted at the server-side document id, run with one more round trip.
"""

from __future__ import annotations

from typing import Any, cast

import httpx
from pydantic import PrivateAttr

from ..expr import Expr
from ..plan import Plan
from .client_core import WebClientCore
from .reference_core import HttpMethod, ReferenceCore
from .reference_core import from_url as _core_from_url


def _url_of(source: dict[str, Any]) -> str:
    return cast(str, ReferenceCore(**source).dispatch("url"))


class _RemoteDoc:
    """A server-side document handle: metadata inline, ops as remote plans."""

    def __init__(self, meta: dict[str, Any], core: "RemoteWebClientCore") -> None:
        object.__setattr__(self, "_meta", meta)
        object.__setattr__(self, "_core", core)

    def __getattr__(self, name: str) -> Any:
        meta = object.__getattribute__(self, "_meta")
        if name in meta:  # title / ok / kind / id
            return meta[name]
        core = object.__getattribute__(self, "_core")
        root = Expr(Plan(root="Document", source={"document_id": meta["id"]}), core)
        return getattr(root, name)

    def __repr__(self) -> str:
        return f"_RemoteDoc({object.__getattribute__(self, '_meta')})"


class RemoteWebClientCore(WebClientCore):
    """A ``WebClientCore`` whose ``execute`` round-trips to ``/execute`` instead
    of running locally. The authoring backing (fetch) is inherited, so the
    surface is unchanged; only execution differs."""

    url: str
    token: str | None = None

    _http: Any = PrivateAttr(default=None)

    def model_post_init(self, ctx: Any) -> None:
        super().model_post_init(ctx)
        self.url = self.url.rstrip("/")
        self._http = httpx.Client()

    def _init_transport(self) -> None:
        """No local transport pool -- execution is a remote round-trip."""

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    # -- execution: one Plan POSTed to /execute ------------------------------
    def execute(self, expr: Any, context: Any = None, *, stream: bool = False) -> Any:
        body: dict[str, Any] = {"plan": expr._plan.model_dump()}
        src = expr._plan.source
        if src and "document_id" in src:
            body["document_id"] = src["document_id"]
        elif src:  # a reference(url)-rooted plan carries its spec
            body["url"] = _url_of(src)
        # a context roots a context-based plan (e.g. ref.resolve()) server-side
        if isinstance(context, _RemoteDoc):
            body["document_id"] = context._meta["id"]
        elif isinstance(context, Expr):  # a client ref/fetch -- send its plan
            body["context_plan"] = context._plan.model_dump()
        resp = self._http.post(
            f"{self.url}/execute", json=body, headers=self._headers()
        )
        if not (200 <= resp.status_code < 300):
            from ..errors import RemoteError

            raise RemoteError(resp.status_code, resp.text[:200])
        return self._deserialize(resp.json()["rows"])

    def _deserialize(self, rows: Any) -> Any:
        if isinstance(rows, dict) and "__doc__" in rows:
            return _RemoteDoc(rows["__doc__"], self)
        if isinstance(rows, list):
            return [self._deserialize(r) for r in rows]
        return rows

    def close(self) -> None:
        if self._http is not None:
            self._http.close()
        super().close()

    # -- server-side sessions ------------------------------------------------
    def session(self, *, ttl: float | None = None, **kw: Any) -> "RemoteSession":
        resp = self._http.post(
            f"{self.url}/sessions", json={"ttl": ttl}, headers=self._headers()
        )
        resp.raise_for_status()
        return RemoteSession(self, resp.json()["id"])

    def close_session(self, sid: str) -> None:
        self._http.delete(f"{self.url}/sessions/{sid}", headers=self._headers())


class RemoteSession:
    """A handle to a server-side session; its fetches thread the session id into
    the plan so the server resolves them through that session. A context manager
    (``with rc.session() as s:``) so the server-side session is always closed."""

    def __init__(self, core: RemoteWebClientCore, sid: str) -> None:
        self._core = core
        self._id = sid
        self._status = "running"

    def ref(self, url: str, method: str = "get", **kw: Any) -> Any:
        spec = _core_from_url(url, cast(HttpMethod, method), **kw).model_dump()
        return Expr(
            Plan(root="Reference", source=spec, session_id=self._id), self._core
        )

    def fetch(self, url: str, **kw: Any) -> Any:
        return self.ref(url, **kw).resolve()

    def close(self) -> None:
        self._core.close_session(self._id)
        self._status = "closed"

    def __enter__(self) -> "RemoteSession":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @property
    def id(self) -> str:
        return self._id

    @property
    def status(self) -> str:
        return self._status


__all__ = ["RemoteWebClientCore", "RemoteSession"]
