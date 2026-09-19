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

from typing import TYPE_CHECKING, Any, Literal, cast

import httpx
from pydantic import PrivateAttr

from ...query.expr import Expr
from ..client import WebClient, _materialize, _seed_urls
from ..engine import Engine
from ..reference import Reference
from .wire import deserialize, reject_sequence, url_of, wire_models  # noqa: F401 (re-exported)

if TYPE_CHECKING:
    from ..crawl import Crawl
    from ..document import Document

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
        return _materialize(deserialize(client, resp.json()["rows"]))

    # -- crawl: a server-side crawl, driven by dispatch ----------------------
    def crawl_post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        """POST to a crawl endpoint and return the server crawl's state (or raise)."""
        resp = self.http.post(
            f"{self.url}{path}",
            json={k: v for k, v in body.items() if v is not None},
            headers=self._headers(),
        )
        _raise_for_body(resp)
        return cast("dict[str, Any]", resp.json())

    def crawl(self, client: Any, seeds: Any, **kwargs: Any) -> "Crawl":
        """Create a server-side crawl and return a :class:`Crawl` handle over it (driven
        with ``run``/``step``/``stream``, each dispatching to the server)."""
        from ..crawl import Crawl

        resolve = kwargs.get("resolve")
        project = kwargs.get("project")
        body: dict[str, Any] = {
            "seeds": _seed_urls(seeds),
            "project": project._plan.model_dump() if isinstance(project, Expr) else None,
            "auto": kwargs.get("auto", True),
            "width": kwargs.get("width", 10),
            "depth": kwargs.get("depth", 3),
            "max_pages": kwargs.get("max_pages", 50),
            "max_frontier": kwargs.get("max_frontier", 10000),
            "same_origin": kwargs.get("same_origin", True),
            "allow_subdomains": kwargs.get("allow_subdomains", True),
            "allow_domains": kwargs.get("allow_domains"),
            "deny_domains": kwargs.get("deny_domains"),
            "allow_countries": kwargs.get("allow_countries"),
            "deny_countries": kwargs.get("deny_countries"),
            "include": kwargs.get("include"),
            "exclude": kwargs.get("exclude"),
            "include_xhr": kwargs.get("include_xhr", True),
            "keywords": kwargs.get("keywords"),
            "obey_robots": kwargs.get("obey_robots", True),
            "browser": kwargs.get("browser", "auto"),
            "resolve": resolve.model_dump() if resolve is not None else None,
        }
        if self.sid:  # a session-scoped crawl runs with the server session's identity
            body["session"] = self.sid
        state = self.crawl_post("/crawls", body)
        crawl = Crawl()
        crawl._client = client
        crawl._crawl_id = state["id"]
        self.adopt_crawl_state(client, crawl, state)
        return crawl

    def advance_crawl(self, client: Any, crawl: "Crawl", op: str, *args: Any) -> "Crawl":
        """Dispatch a remote ``step``/``run`` and adopt the returned state into ``crawl``."""
        body: dict[str, Any] = {}
        if op == "step" and args and args[0] is not None:
            from ..crawl import Edge

            body["select"] = [e.url if isinstance(e, Edge) else str(e) for e in args[0]]
        state = self.crawl_post(f"/crawls/{crawl._crawl_id}/{op}", body)
        self.adopt_crawl_state(client, crawl, state)
        return crawl

    def adopt_crawl_state(self, client: Any, crawl: "Crawl", state: dict[str, Any]) -> None:
        """Refresh a crawl handle's mirrored state from the server so its local reads are current."""
        from ..crawl import CrawlConfig, Edge, Failure

        crawl.config = CrawlConfig.model_validate(state.get("config", {}))
        crawl.scope = state.get("scope", "")
        crawl.status = state.get("status", "running")
        crawl.frontier = [Edge(**e) for e in state.get("frontier", [])]
        crawl.pages = [deserialize(client, p) for p in state.get("pages", [])]
        crawl.history = [Edge(**e) for e in state.get("history", [])]
        crawl.failures = [Failure(**f) for f in state.get("failures", [])]
        crawl._seen = set(state.get("seen", []))

    # -- server-side sessions ------------------------------------------------
    def open_session(self, ttl: float | None) -> str:
        """Create a server-side session and return its id."""
        resp = self.http.post(f"{self.url}/sessions", json={"ttl": ttl}, headers=self._headers())
        resp.raise_for_status()
        return cast(str, resp.json()["id"])

    def close_session(self, sid: str) -> None:
        self.http.delete(f"{self.url}/sessions/{sid}", headers=self._headers())

    def close(self) -> None:
        if self.owns_http:  # a session shares the root's http -- only the root closes it
            self.http.close()


class RemoteWebClientCore(WebClient):
    """A ``WebClient`` in ``"remote"`` dispatch mode: it holds a :class:`RemoteConnection`
    (``_conn``) and delegates ``execute`` / ``crawl`` / ``session`` to it, so every
    server-needing op POSTs one Plan to the service. A thin shell over the connection --
    the transport logic lives in :class:`RemoteConnection`, not here; only the dispatch
    mode differs from a local client."""

    url: str
    token: str | None = None

    _conn: Any = PrivateAttr(default=None)  # the RemoteConnection (transport)
    _remote_hops: int = PrivateAttr(default=0)  # per-op round-trips (chattiness)
    _nagged: bool = PrivateAttr(default=False)  # warned about .lazy once

    def model_post_init(self, ctx: Any) -> None:
        super().model_post_init(ctx)
        self._engine._mode = "remote"  # the mode is an engine property
        self.url = self.url.rstrip("/")
        # bound every round-trip by the client's timeout so a hung service can't block forever.
        self._conn = RemoteConnection(self.url, self.token, self.timeout)

    def _init_transport(self) -> None:
        """No local transport pool -- execution is a remote round-trip. Still bind a
        pool-less :class:`Engine` so the bus/loop are available like any client."""
        self._engine = Engine(self.browser_config, transport=False)

    def execute(self, expr: Any, context: Any = None, *, stream: bool = False) -> Any:
        return self._conn.execute(self, expr, context, stream=stream)

    def crawl(self, seeds: Any, **kwargs: Any) -> "Crawl":
        return cast("Crawl", self._conn.crawl(self, seeds, **kwargs))

    def _advance_crawl(self, crawl: "Crawl", op: str, *args: Any) -> "Crawl":
        """Reached from ``Crawl._remote_call`` for a remote ``step``/``run``."""
        return cast("Crawl", self._conn.advance_crawl(self, crawl, op, *args))

    def release(self, doc: "Document") -> None:
        """No-op on remote: the server owns its transport pool and reclaims pages."""

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
        super().close()

    def session(  # type: ignore[override]  # remote sessions are a distinct core
        self, *, ttl: float | None = None, **kw: Any
    ) -> "RemoteWebSessionCore":
        sid = self._conn.open_session(ttl)
        return RemoteWebSessionCore(url=self.url, token=self.token)._bind(self, sid)

    def close_session(self, sid: str) -> None:  # kept for symmetry / callers
        self._conn.close_session(sid)


class RemoteWebSessionCore(RemoteWebClientCore):
    """A server-side session as a real core: a remote client whose ``_conn`` carries the
    server session id (so every plan resolves through that session) and SHARES the
    parent's http. ``session.ref(url)`` / ``session.fetch(url)`` dispatch like the
    client's, only scoped. A context manager -- ``with rc.session() as s: ...`` deletes
    the server session on exit."""

    status: Literal["running", "closed"] = "running"

    _parent: Any = PrivateAttr(default=None)  # the RemoteWebClientCore
    _sid: str = PrivateAttr(default="")  # the server session id

    def model_post_init(self, ctx: Any) -> None:
        # skip RemoteWebClientCore.model_post_init (which would open its own connection);
        # the sid-carrying, http-sharing connection is built in ``_bind``.
        WebClient.model_post_init(self, ctx)
        self._engine._mode = "remote"

    def _bind(self, parent: "RemoteWebClientCore", sid: str) -> "RemoteWebSessionCore":
        self._parent = parent
        self._sid = sid
        # share the parent's http; carry the sid so the connection threads it into plans.
        self._conn = RemoteConnection(
            parent.url, parent.token, self.timeout, http=parent._conn.http, sid=sid
        )
        self.url, self.token = parent.url, parent.token
        return self

    @property
    def id(self) -> str:
        return self._sid

    def close(self) -> None:
        if self.status == "closed":
            return
        self._parent._conn.close_session(self._sid)
        self.status = "closed"

    def __enter__(self) -> "RemoteWebSessionCore":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


__all__ = ["RemoteConnection", "RemoteWebClientCore", "RemoteWebSessionCore"]
