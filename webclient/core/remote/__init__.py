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
from ..document import Document
from ..reference import Reference

if TYPE_CHECKING:
    from ..crawl import Crawl


def _url_of(source: dict[str, Any]) -> str:
    return cast(str, Reference(**source).dispatch("url"))


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
        from ..crawl.models import Edge
        from ..document.models import (
            Element,
            Metadata,
            Signal,
            Structure,
            Transport,
        )

        models: list[type[Any]] = [
            Transport, Metadata, Structure, Signal, Element,
            Edge, Event, NavigationEvent, NetworkEvent, ConsoleEvent,
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

    # -- crawl / sitemap: run server-side via the service endpoints ----------
    # A crawl is client-held state driving many fetches; over the wire that is a
    # server-side job (the same shape as a server-side ``session``), so remote
    # crawl/sitemap POST to the service's /crawl and /sitemap endpoints and hand
    # back a finished :class:`Crawl` -- run to completion in one round-trip
    # (turn-based ``step()`` steering is a local-client feature).
    def _remote_crawl(self, path: str, body: dict[str, Any]) -> "Crawl":
        from ..crawl import Crawl, Edge

        sid = getattr(self, "_sid", "")
        if sid:  # a session-scoped crawl runs with the server session's identity
            body["session"] = sid
        payload = {k: v for k, v in body.items() if v is not None}
        resp = self._http.post(
            f"{self.url}{path}", json=payload, headers=self._headers()
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
        data = resp.json()
        # a remote crawl's Documents stay server-side; the wire carries a lean
        # per-page record (url/status/kind/title), kept as ``.pages`` plain dicts.
        core = Crawl(
            status="closed",  # the server ran it to completion
            pages=list(data.get("pages", [])),
            frontier=[Edge(**e) for e in data.get("frontier", [])],
        )
        return core.bind(self)

    def crawl(
        self,
        seeds: Any,
        *,
        scope: str | None = None,
        auto: bool = True,
        width: int = 10,
        depth: int = 3,
        max_pages: int = 50,
        max_frontier: int = 10000,
        same_origin: bool = True,
        obey_robots: bool = True,
        browser: bool = True,
        resolve: Any = None,
        keywords: list[str] | None = None,
        include: str | None = None,
        exclude: str | None = None,
    ) -> "Crawl":
        """A remote crawl runs to completion server-side (one round-trip) and
        returns a finished :class:`Crawl`. ``auto`` (always on server-side) and
        ``scope`` (derived from the seed host) are accepted for signature parity
        with the local client but not sent -- use a local client for turn-based
        steering. ``browser`` (default on, as locally) and ``resolve`` (a policy
        bundle) are sent so the server renders / fetches under the same policy."""
        urls = _seed_urls(seeds)
        return self._remote_crawl(
            "/crawl",
            {
                "url": urls[0] if urls else "",
                "width": width,
                "depth": depth,
                "max_pages": max_pages,
                "max_frontier": max_frontier,
                "same_origin": same_origin,
                "obey_robots": obey_robots,
                "browser": browser,
                "resolve": resolve.model_dump() if resolve is not None else None,
                "keywords": keywords,
                "include": include,
                "exclude": exclude,
            },
        )

    def sitemap(
        self,
        url: Any,
        *,
        depth: int = 2,
        width: int = 20,
        max_pages: int = 1000,
        use_sitemap_xml: bool = True,
        browser: bool = False,
        resolve: Any = None,
    ) -> "Crawl":
        target = url if isinstance(url, str) else str(getattr(url, "url", url))
        return self._remote_crawl(
            "/sitemap",
            {
                "url": target, "depth": depth, "width": width, "max_pages": max_pages,
                "browser": browser,
                "resolve": resolve.model_dump() if resolve is not None else None,
            },
        )

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
