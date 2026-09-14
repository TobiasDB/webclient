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

from typing import Any, cast

import httpx
from pydantic import PrivateAttr

from ...query.expr import Expr
from ...query.plan import Plan
from ..client import WebClientCore, _materialize
from ..document import DocumentCore
from ..reference import HttpMethod, ReferenceCore
from ..reference import from_url as _core_from_url


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
