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
from ..document import Document
from ..reference import Reference

if TYPE_CHECKING:
    from ..crawl import Crawl


def _url_of(source: dict[str, Any]) -> str:
    return cast(str, Reference(**source).dispatch("url"))


def _reject_sequence(expr: Any) -> None:
    """A ``.step(...)`` sequence holds ONE live page across ordered actions. A COMPLETE
    stepful plan -- one that ends in ``.project()`` -- produces DATA: its held page lives
    entirely inside a single server-side ``/execute`` evaluation and never crosses the
    wire, so it runs remotely like any other data-producing browser plan. An OPEN stepful
    plan (one that would hand back a live page/element -- e.g. it ends in
    ``select``/``select_all``) has no remote representation for that held page, so it
    stays engine-local: fail clearly, and tell the caller to close it with ``.project()``."""
    plan = getattr(expr, "_plan", None)
    gets = [s for s in (getattr(plan, "steps", None) or ()) if s.kind == "get"]
    if not any(s.name == "step" for s in gets):
        return  # no sequence -- nothing to guard
    if gets and gets[-1].name == "project":
        return  # a complete, data-producing sequence -- safe to run server-side
    raise NotImplementedError(
        "an OPEN .step(...) sequence (one that returns a live page/element) runs only on "
        "a local client; end it with .project() to produce data and run it remotely"
    )


_WIRE_MODELS_CACHE: "dict[str, type[Any]] | None" = None


def _wire_models() -> "dict[str, type[Any]]":
    """Name -> class for the value models an op can return over the wire (built
    once), so ``_deserialize`` rebuilds a real ``Transport``/``Metadata``/… facet
    model from a tagged ``{"__model__": ...}`` payload."""
    global _WIRE_MODELS_CACHE
    if _WIRE_MODELS_CACHE is None:
        from ...models import (
            ActionEvent,
            ConsoleEvent,
            DOMUpdateEvent,
            Event,
            NavigationEvent,
            NetworkEvent,
            PlanEvent,
        )
        from ..client.models import Robots
        from ..crawl.models import Edge
        from ..document.models import (
            Element,
            Metadata,
            Flag,
            PageCard,
            Signal,
            Structure,
            Transport,
        )

        models: list[type[Any]] = [
            Transport, Metadata, Structure, Signal, Flag, Element, PageCard,
            Edge, Robots, Event, NavigationEvent, NetworkEvent, ConsoleEvent,
            DOMUpdateEvent, ActionEvent, PlanEvent,
        ]
        _WIRE_MODELS_CACHE = {m.__name__: m for m in models}
    return _WIRE_MODELS_CACHE


class RemoteWebClientCore(WebClient):
    """A ``WebClient`` in ``"remote"`` mode: its ``execute`` POSTs one Plan to
    ``/execute`` instead of running locally, and ``WebCore``'s remote dispatcher
    turns every server-needing op into such a POST. The surface is unchanged; only
    the dispatch mode differs."""

    url: str
    token: str | None = None

    _http: Any = PrivateAttr(default=None)
    _remote_hops: int = PrivateAttr(default=0)  # per-op round-trips (chattiness)
    _nagged: bool = PrivateAttr(default=False)  # warned about .lazy once

    def model_post_init(self, ctx: Any) -> None:
        super().model_post_init(ctx)
        self._engine._mode = "remote"  # the mode is an engine property
        self.url = self.url.rstrip("/")
        # bound every round-trip by the client's timeout so a hung service can't
        # block the caller forever.
        self._http = httpx.Client(timeout=self.timeout)

    def _init_transport(self) -> None:
        """No local transport pool -- execution is a remote round-trip. Still bind a
        pool-less :class:`Engine` so the bus/loop are available like any client."""
        self._engine = Engine(self.browser_config, transport=False)

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    # -- execution: one Plan POSTed to /execute ------------------------------
    def execute(self, expr: Any, context: Any = None, *, stream: bool = False) -> Any:
        _reject_sequence(expr)
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
        elif isinstance(context, Reference):
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
            ref = Reference(**rows["__ref__"])
            ref._client = self
            return ref
        if isinstance(rows, dict) and "__model__" in rows:
            # rebuild the real value model (Summary/SearchResult/Element/…) so a
            # remote result has the same type as a local one (s.title, not s["title"]).
            model = _wire_models().get(rows["__model__"])
            data = rows.get("data", {})
            return model.model_validate(data) if model is not None else data
        if isinstance(rows, list):
            return [self._deserialize(r) for r in rows]
        return rows

    def _doc_handle(self, meta: dict[str, Any]) -> Document:
        """A server-side document as a real ``Document``: id/kind/ok are inline
        (``status_code`` set so the ``ok`` property agrees), content ops round-trip
        (``_remote_handle``)."""
        doc = Document(
            url="",
            kind=meta.get("kind", "html"),
            status_code=200 if meta.get("ok", True) else 502,
        )
        doc.id = doc.name = meta["id"]
        doc._client = self
        doc._remote_handle = True
        return doc

    # -- crawl: a server-side crawl, driven by dispatch ----------------------
    # A crawl is stateful (it owns a frontier + drives many fetches), so it lives on
    # the server (like a session) addressed by id. ``crawl()`` creates it; the Crawl
    # core's ``step``/``run`` dispatch here (``_advance_crawl``) to advance it and
    # refresh the handle's mirror -- so turn-based stepping AND streaming work
    # remotely, not just run-to-completion. ``sitemap``/``robots`` are ordinary
    # dispatched IO ops (they ride ``/execute`` like ``fetch``), no override needed.
    def _crawl_post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        """POST to a crawl endpoint and return the server crawl's state (or raise)."""
        resp = self._http.post(
            f"{self.url}{path}",
            json={k: v for k, v in body.items() if v is not None},
            headers=self._headers(),
        )
        if not (200 <= resp.status_code < 300):
            from ...errors import RemoteError, WebError

            err: WebError | None = None
            try:
                data = resp.json()
                if isinstance(data, dict) and isinstance(data.get("error"), dict):
                    err = WebError(**data["error"])
            except Exception:
                pass
            raise RemoteError(resp.status_code, resp.text[:200], error=err)
        return cast("dict[str, Any]", resp.json())

    def crawl(self, seeds: Any, **kwargs: Any) -> "Crawl":
        """Create a server-side crawl and return a :class:`Crawl` handle over it. Unlike
        before, it does NOT run to completion -- drive it with ``crawl.run()`` (batch),
        ``crawl.step(select)`` (turn-based) or ``for card in crawl.stream()``, exactly as
        a local crawl: each dispatches to the server. Accepts the local ``crawl`` wire
        knobs (budget / scope / browser / resolve / keywords); a custom ``project`` is
        local-only, so remote pages are always :class:`PageCard`\\ s."""
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
        sid = getattr(self, "_sid", "")
        if sid:  # a session-scoped crawl runs with the server session's identity
            body["session"] = sid
        state = self._crawl_post("/crawls", body)
        crawl = Crawl()
        crawl._client = self
        crawl._crawl_id = state["id"]
        self._adopt_crawl_state(crawl, state)
        return crawl

    def _advance_crawl(self, crawl: "Crawl", op: str, *args: Any) -> "Crawl":
        """Dispatch a remote ``step``/``run``: POST to the server-side crawl, then adopt
        the returned state into ``crawl``'s mirror. ``step``'s selection is sent as a
        list of URLs (the server matches its own frontier by URL, or adds new ones)."""
        body: dict[str, Any] = {}
        if op == "step" and args and args[0] is not None:
            from ..crawl import Edge

            body["select"] = [e.url if isinstance(e, Edge) else str(e) for e in args[0]]
        state = self._crawl_post(f"/crawls/{crawl._crawl_id}/{op}", body)
        self._adopt_crawl_state(crawl, state)
        return crawl

    def _adopt_crawl_state(self, crawl: "Crawl", state: dict[str, Any]) -> None:
        """Refresh a crawl handle's mirrored state from the server (config / scope /
        status / frontier / pages / history / seen), so its local reads are current.
        ``pages`` is deserialised the usual way -- a PageCard/model, a Document handle,
        or a scalar/dict -- so a custom projection survives the round-trip."""
        from ..crawl import CrawlConfig, Edge, Failure

        crawl.config = CrawlConfig.model_validate(state.get("config", {}))
        crawl.scope = state.get("scope", "")
        crawl.status = state.get("status", "running")
        crawl.frontier = [Edge(**e) for e in state.get("frontier", [])]
        crawl.pages = [self._deserialize(p) for p in state.get("pages", [])]
        crawl.history = [Edge(**e) for e in state.get("history", [])]
        crawl.failures = [Failure(**f) for f in state.get("failures", [])]
        crawl._seen = set(state.get("seen", []))

    def release(self, doc: Document) -> None:
        """No-op on remote: the server owns its transport pool and reclaims pages
        (the doc store is LRU-bounded); there is no local page to return."""

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
        WebClient.model_post_init(self, ctx)
        self._engine._mode = "remote"  # the mode is an engine property (parent's shares it post-bind)

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
