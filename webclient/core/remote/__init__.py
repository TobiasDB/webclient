"""Remote backend: a ``WebClientCore`` in ``"remote"`` dispatch mode.

Remote is not a separate client or a special surface -- it is the SAME cores in a
third dispatch mode (see ``WebCore._dispatch_mode``): every op that needs the
server (IO ops, and every content op on a server-side document handle) is
recorded onto the core's remote root and ``collect``ed in one round-trip to a
``webclient.service`` app, so a ``WebClient`` over it builds the very same plans
with no local browser or lxml -- only httpx + pydantic. A fetched document comes
back as a real ``DocumentCore`` (a lightweight handle: id/kind/ok inline, its
content ops round-trip), a reference as a real ``ReferenceCore`` -- no bespoke
handle type, no interface exceptions. Batch a chain/fan-out with ``.lazy``.
"""

from __future__ import annotations

from typing import Any, Literal, cast

import httpx
from pydantic import PrivateAttr

from ...query.expr import Expr
from ..client import WebClientCore, _materialize
from ..document import DocumentCore
from ..reference import ReferenceCore


def _url_of(source: dict[str, Any]) -> str:
    return cast(str, ReferenceCore(**source).dispatch("url"))


class RemoteWebClientCore(WebClientCore):
    """A ``WebClientCore`` in ``"remote"`` mode: its ``execute`` POSTs one Plan to
    ``/execute`` instead of running locally, and ``WebCore``'s remote dispatcher
    turns every server-needing op into such a POST. The surface is unchanged; only
    the dispatch mode differs."""

    url: str
    token: str | None = None

    _http: Any = PrivateAttr(default=None)
    _mode: str = PrivateAttr(default="remote")
    _remote_hops: int = PrivateAttr(default=0)  # per-op round-trips (chattiness)
    _nagged: bool = PrivateAttr(default=False)  # warned about .lazy once

    def model_post_init(self, ctx: Any) -> None:
        super().model_post_init(ctx)
        self._mode = "remote"
        self.url = self.url.rstrip("/")
        # bound every round-trip by the client's timeout so a hung service can't
        # block the caller forever.
        self._http = httpx.Client(timeout=self.timeout)

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
        elif src:  # a reference-rooted plan carries its spec
            body["url"] = _url_of(src)
        # a context roots a context-based plan (e.g. plan.collect(rc.ref(url)))
        # server-side: a server document handle by id, a reference by its spec, a
        # recorded Expr by its plan.
        if getattr(context, "_remote_handle", False):
            body["document_id"] = context.id
        elif isinstance(context, ReferenceCore):
            body["url"] = context.dispatch("url")
        elif isinstance(context, Expr):
            body["context_plan"] = context._plan.model_dump()
        resp = self._http.post(
            f"{self.url}/execute", json=body, headers=self._headers()
        )
        if not (200 <= resp.status_code < 300):
            from ...errors import RemoteError, WebError

            err: WebError | None = None
            try:  # the service sends {"error": {type, message, status_code, ...}}
                payload = resp.json()
                if isinstance(payload, dict) and isinstance(payload.get("error"), dict):
                    err = WebError(**payload["error"])
            except Exception:
                pass
            raise RemoteError(resp.status_code, resp.text[:200], error=err)
        # the service returns clean JSON; rebuild real cores (a Document handle, a
        # Reference) and wrap a scalar leaf into a Field, so remote and local
        # ``collect()`` agree on the result type.
        return _materialize(self._deserialize(resp.json()["rows"]))

    def _deserialize(self, rows: Any) -> Any:
        if isinstance(rows, dict) and "__doc__" in rows:
            return self._doc_handle(rows["__doc__"])
        if isinstance(rows, dict) and "__ref__" in rows:
            ref = ReferenceCore(**rows["__ref__"])
            ref._client = self
            return ref
        if isinstance(rows, list):
            return [self._deserialize(r) for r in rows]
        return rows

    def _doc_handle(self, meta: dict[str, Any]) -> DocumentCore:
        """A server-side document as a real ``DocumentCore``: id/kind/ok are inline
        (``status_code`` set so the ``ok`` property agrees), content ops round-trip
        (``_remote_handle``)."""
        doc = DocumentCore(
            url="",
            kind=meta.get("kind", "html"),
            status_code=200 if meta.get("ok", True) else 502,
        )
        doc.id = doc.name = meta["id"]
        doc._client = self
        doc._remote_handle = True
        return doc

    def close(self) -> None:
        if self._http is not None:
            self._http.close()
        super().close()

    # -- server-side sessions ------------------------------------------------
    def session(  # type: ignore[override]  # remote sessions are a distinct core
        self, *, ttl: float | None = None, **kw: Any
    ) -> "RemoteWebSessionCore":
        resp = self._http.post(
            f"{self.url}/sessions", json={"ttl": ttl}, headers=self._headers()
        )
        resp.raise_for_status()
        return RemoteWebSessionCore(url=self.url, token=self.token)._bind(
            self, resp.json()["id"]
        )

    def close_session(self, sid: str) -> None:
        self._http.delete(f"{self.url}/sessions/{sid}", headers=self._headers())


class RemoteWebSessionCore(RemoteWebClientCore):
    """A server-side session as a real core -- no bespoke handle: it is a remote
    client that threads its server session id into every plan (so the service
    resolves through that session) and shares the parent client's http. So
    ``session.ref(url)`` / ``session.fetch(url)`` dispatch exactly like the
    client's, only scoped. A context manager: ``with rc.session() as s: ...``
    deletes the server session on exit."""

    status: Literal["running", "closed"] = "running"

    _parent: Any = PrivateAttr(default=None)  # the RemoteWebClientCore
    _sid: str = PrivateAttr(default="")  # the server session id

    def model_post_init(self, ctx: Any) -> None:
        # share the parent's http (set in ``_bind``) -- don't open our own; skip
        # RemoteWebClientCore.model_post_init (which would) and just mark the mode.
        WebClientCore.model_post_init(self, ctx)
        self._mode = "remote"

    def _bind(
        self, parent: "RemoteWebClientCore", sid: str
    ) -> "RemoteWebSessionCore":
        self._parent = parent
        self._sid = sid
        self._http = parent._http  # share the connection
        self.url, self.token = parent.url, parent.token
        return self

    @property
    def id(self) -> str:
        return self._sid

    def execute(self, expr: Any, context: Any = None, *, stream: bool = False) -> Any:
        # thread the server session id into the plan so the service resolves
        # through this session; then POST via the remote client machinery.
        scoped = Expr(
            expr._plan.model_copy(update={"session_id": self._sid}), expr._client
        )
        return super().execute(scoped, context, stream=stream)

    def close(self) -> None:
        if self.status == "closed":
            return
        self._parent.close_session(self._sid)
        self.status = "closed"

    def __enter__(self) -> "RemoteWebSessionCore":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


__all__ = ["RemoteWebClientCore", "RemoteWebSessionCore"]
