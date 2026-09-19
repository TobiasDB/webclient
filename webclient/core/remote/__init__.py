"""Remote backend: a ``WebClient`` in ``"remote"`` dispatch mode.

Remote is not a separate client or a special surface -- it is the SAME cores in a
third dispatch mode (see ``WebCore._dispatch_mode``): every op that needs the
server (IO ops, and every content op on a server-side document handle) is
recorded onto the core's remote root and ``collect``ed in one round-trip to a
``webclient.service`` app, so a ``WebClient`` over it builds the very same plans
with no local browser or lxml -- only httpx + pydantic. A fetched document comes
back as a real ``Document`` (a lightweight handle: id/kind/ok inline, its
content ops round-trip), a reference as a real ``Reference`` -- no bespoke
handle type, no interface exceptions. Batch a chain/fan-out with ``.lazy``.
"""

from __future__ import annotations

from typing import Any, cast

import httpx

from ...query.expr import Expr
from ..client import WebClient, _materialize
from ..engine import Engine
from ..reference import Reference
from .wire import deserialize, reject_sequence, url_of, wire_models  # noqa: F401 (re-exported)

#: back-compat alias -- the guard now lives in :mod:`.wire`.
_reject_sequence = reject_sequence


def _raise_for_body(resp: Any) -> None:
    """Turn a non-2xx service response into a structured ``RemoteError``."""
    if 200 <= resp.status_code < 300:
        return
    from ...errors import RemoteError, WebError

    err: WebError | None = None
    try:  # the service sends {"error": {type, message, status_code, ...}}
        payload = resp.json()
        if isinstance(payload, dict) and isinstance(payload.get("error"), dict):
            err = WebError(**payload["error"])
    except Exception:
        pass
    raise RemoteError(resp.status_code, resp.text[:200], error=err)


class RemoteConnection:
    """The remote TRANSPORT a client uses in ``"remote"`` dispatch mode: the HTTP
    connection to a :mod:`webclient.service` app plus the plan-POST + server-side
    crawl/session drivers. Held on the client as ``_conn``; the client delegates its
    ``execute`` / ``crawl`` / ``session`` to it. This is the substance a remote client
    is -- a dispatch mode + this connection + the :mod:`.wire` (de)serialisation -- not
    a bespoke ``WebClient`` subtype. A server-session connection carries a ``sid`` (its
    plans resolve through that session) and SHARES the parent's http."""

    def __init__(
        self, url: str, token: str | None, timeout: float, *,
        http: Any = None, sid: str = "",
    ) -> None:
        self.url = url.rstrip("/")
        self.token = token
        self.sid = sid  # a server session id ("" = the connection itself)
        self.owns_http = http is None  # only the root connection closes the http
        self.http = http if http is not None else httpx.Client(timeout=timeout)

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    # -- execution: one Plan POSTed to /execute ------------------------------
    def execute(self, client: Any, expr: Any, context: Any = None, *, stream: bool = False) -> Any:
        reject_sequence(expr)
        if self.sid:  # a session-scoped plan resolves through the server session
            expr = Expr(expr._plan.model_copy(update={"session_id": self.sid}), expr._client)
        body: dict[str, Any] = {"plan": expr._plan.model_dump()}
        src = expr._plan.source
        if src and "document_id" in src:
            body["document_id"] = src["document_id"]
        elif src:  # a reference-rooted plan carries its spec
            body["url"] = url_of(src)
        # a context roots a context-based plan (e.g. plan.collect(rc.ref(url))) server-side:
        # a server document handle by id, a reference by its spec, a recorded Expr by its plan.
        if getattr(context, "_remote_handle", False):
            body["document_id"] = context.id
        elif isinstance(context, Reference):
            body["url"] = context.dispatch("url")
        elif isinstance(context, Expr):
            body["context_plan"] = context._plan.model_dump()
        resp = self.http.post(f"{self.url}/execute", json=body, headers=self._headers())
        _raise_for_body(resp)
        # rebuild real cores (a Document handle, a Reference) + wrap a scalar leaf into a
        # Field, so remote and local ``collect()`` agree on the result type.
        result = _materialize(deserialize(client, resp.json()["rows"]))
        # 2d: stamp the producing plan on each fresh server-side handle so a later content
        # op can replay it if the server evicts the handle. Only a PRODUCER plan (a
        # fetch/resolve, rooted at the client or a reference) reproduces a handle; a
        # content-op plan (Document root) does not, so it is left without a source.
        if expr._plan.root in ("WebClient", "Reference"):
            for doc in result if isinstance(result, list) else [result]:
                if getattr(doc, "_remote_handle", False):
                    doc._remote_source = expr
        return result

    # a remote crawl is no longer a server-side object driven over a bespoke wire: it
    # runs as one ``WebClient.crawl(...).run().pages`` plan through ``execute`` (see
    # ``Crawl._remote_call``), so there is no crawl transport here.

    # -- server-side sessions ------------------------------------------------
    def open_session(self, ttl: float | None) -> str:
        """Create a server-side session and return its id."""
        resp = self.http.post(f"{self.url}/sessions", json={"ttl": ttl}, headers=self._headers())
        resp.raise_for_status()
        return cast(str, resp.json()["id"])

    def close_session(self, sid: str) -> None:
        self.http.delete(f"{self.url}/sessions/{sid}", headers=self._headers())

    def close(self) -> None:
        if self.sid:  # a server session -- dispose it server-side (its http is shared)
            try:
                self.close_session(self.sid)
            except Exception:  # noqa: BLE001 - best-effort dispose on close
                pass
        elif self.owns_http:  # the root connection owns the http
            self.http.close()


def connect(client: "WebClient", url: str, token: str | None = None) -> "WebClient":
    """Put ``client`` into ``"remote"`` dispatch mode over the service at ``url``: swap in
    a pool-less :class:`Engine` (no local transport) and a :class:`RemoteConnection` it
    delegates to. So a remote client is just a ``WebClient`` in remote mode + this
    connection -- no bespoke subtype. The base client's ``execute``/``crawl``/``session``/
    ``close`` route through ``self._conn`` when it is set (see :class:`WebClient`)."""
    client._engine = Engine(client.browser_config, transport=False)
    client._engine._mode = "remote"
    client._conn = RemoteConnection(url, token, client.timeout)
    return client


def open_remote_session(parent: "WebClient", ttl: float | None) -> RemoteConnection:
    """Open a server-side session on ``parent``'s connection and return a child
    :class:`RemoteConnection` that carries its id + shares the parent's http -- so the
    session's plans thread the server session id, and closing it disposes the session."""
    conn = cast(RemoteConnection, parent._conn)
    sid = conn.open_session(ttl)
    return RemoteConnection(conn.url, conn.token, parent.timeout, http=conn.http, sid=sid)


__all__ = ["RemoteConnection", "connect", "open_remote_session"]
