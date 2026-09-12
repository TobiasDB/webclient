"""RemoteWebClientCore: the remote backend for the clients (PLAN §7).

Remote is not a separate client -- it is a different *core* behind the same
``WebClient``/``AsyncWebClient``. Its ``execute`` runs a plan on a service
over HTTP instead of locally; everything else (the facade helpers, the lazy
``Document``/``Reference`` interface, ``Session``) is backend-agnostic::

    rc = WebClient(core=RemoteWebClientCore("https://host", token="secret"))
    doc = rc.fetch("https://example.com")          # a lazy handle (id + meta)
    print(rc.execute(doc.render("markdown")))      # one POST /execute
    rows = rc.execute(ref.resolve().select_all(".card")
                      .extract(t=doc.select(".t").attr("text")).project(),
                      rc.ref("https://example.com"))

A Document produced server-side crosses the wire as a handle
(``{"__doc__": meta}``); the client rehydrates it into a shallow
:class:`_RemoteDoc` that carries the metadata and acts as a lazy ``Document``
root (a document-source plan) for further ops. No ``Document`` object ever
crosses the wire. The core needs only httpx + pydantic -- no lxml, no
playwright; it shares loop lifecycle with the local core via
:class:`webclient.core.base.EngineCore`, and that loop is created only when a
*sync* client drives it.
"""
from __future__ import annotations

import threading
from typing import Any

from .models import Document, Reference
from .expr import Expr, Plan, lazy
from .base import EngineCore


class RemoteError(RuntimeError):
    """A service call failed (non-2xx from the remote API)."""


class _RemoteDoc:
    """A server-side document handle: cheap metadata (no round trip) plus a
    bound lazy ``Document`` root. Op-building (``select``/``render``/``attr``/
    …) delegates to the lazy interface; run the chain with ``client.execute``."""

    __slots__ = ("id", "kind", "status_code", "final_url", "session_id",
                 "_title", "_expr")

    def __init__(self, core: "RemoteWebClientCore", meta: dict[str, Any]) -> None:
        self.id = meta["id"]
        self.kind = meta.get("kind")
        self.status_code = meta.get("status_code")
        self.final_url = meta.get("final_url")
        self.session_id = meta.get("session_id")
        self._title = meta.get("title")
        self._expr = lazy(Document, client=core, plan=Plan(
            root="Document", source={"document_id": self.id}))

    @property
    def ok(self) -> bool:
        return bool(self.status_code and 200 <= self.status_code < 300)

    @property
    def title(self) -> Any:
        return self._title

    def __getattr__(self, name: str) -> Any:
        # op-building (select/select_all/attr/render/click/…) records on the
        # bound lazy Document; metadata attrs above are served directly.
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self._expr, name)

    def __repr__(self) -> str:
        return f"RemoteDocument(id={self.id!r}, kind={self.kind!r})"


class RemoteWebClientCore(EngineCore):
    """A core whose ``resolve``/``execute`` run on a service over HTTP.
    Async-native: an ``AsyncWebClient`` awaits it on the caller's loop; a sync
    ``WebClient`` drives it on the engine loop ``EngineCore`` creates on demand."""

    def __init__(self, base_url: str, *, token: str | None = None,
                 transport: Any = None, timeout: float = 60.0) -> None:
        self._base = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {token}"} if token else {}
        self._timeout = timeout
        self._transport = transport
        self._http: Any = None                 # httpx.AsyncClient, lazy on loop
        self._loop: Any = None                 # engine loop, only for sync use
        self._loop_lock = threading.Lock()
        self._closed = False

    def _client_http(self) -> Any:
        if self._http is None:
            import httpx
            self._http = httpx.AsyncClient(
                base_url=self._base, headers=self._headers,
                timeout=self._timeout, transport=self._transport)
        return self._http

    async def _acall(self, method: str, path: str, **kw: Any) -> Any:
        resp = await self._client_http().request(method, path, **kw)
        if resp.status_code >= 400:
            raise RemoteError(f"{method} {path} -> {resp.status_code}: "
                              f"{resp.text[:200]}")
        return resp.json()

    # -- the two primitives --------------------------------------------------
    async def execute(self, expr: Any, context: Any = None) -> Any:
        """Submit a plan to ``/execute`` and rehydrate the result. A document
        result comes back as a handle; a document-source chain self-carries
        its id, so no context is needed."""
        plan = expr if isinstance(expr, Plan) else expr._plan
        body: dict[str, Any] = {"plan": plan.model_dump()}
        url, sid = self._context_ref(context)
        if url is not None:
            body["url"] = url
        if sid:
            body["session_id"] = sid
        return self._hydrate((await self._acall("POST", "/execute", json=body))["rows"])

    @staticmethod
    def _context_ref(context: Any) -> tuple[Any, Any]:
        """The (url, session_id) for an execute context: a RemoteRef-like, or a
        lazy reference root (``wc.ref(url)``) whose plan carries the spec."""
        if context is None:
            return None, None
        if isinstance(context, Expr):                      # a lazy reference root
            src = context._plan.source
            return (Reference(**src).url if src else None), None
        sid = getattr(getattr(context, "_session", None), "id", None) \
            or getattr(context, "session_id", None)
        return getattr(context, "url", None), sid

    async def resolve(self, ref: Any, *, browser: bool = False,
                      session: Any = None, optional: bool = False,
                      **options: Any) -> Any:
        """Resolve a reference server-side; returns a lazy handle."""
        from .engine import fetch_expr
        return await self.execute(
            fetch_expr(browser=browser, optional=optional, **options), ref)

    async def astream(self, expr: Any, context: Any = None) -> Any:
        """No server push: run the plan and yield the resulting rows."""
        result = await self.execute(expr, context)
        for row in (result if isinstance(result, list) else [result]):
            yield row

    def _hydrate(self, value: Any) -> Any:
        if isinstance(value, dict):
            if "__doc__" in value:
                return _RemoteDoc(self, value["__doc__"])
            return {k: self._hydrate(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self._hydrate(v) for v in value]
        return value

    # -- sessions (backend-agnostic Session bound to this core) --------------
    def session(self, *, ttl: float | None = None, keep_alive: bool = False,
                headers: dict[str, str] | None = None) -> Any:
        from .engine import Session
        data = self._ensure_loop().run(self._acall("POST", "/sessions", json={
            "ttl": ttl, "keep_alive": keep_alive, "headers": headers or {}}))
        sess = Session(id=data["id"], status=data.get("status", "running"),
                       ttl=ttl, keep_alive=keep_alive, headers=headers or {},
                       expires_at=data.get("expires_at"))
        sess._client = self
        return sess

    def _teardown_session(self, session: Any) -> None:
        self._ensure_loop().run(
            self._acall("DELETE", f"/sessions/{session.id}"))

    # -- registry is server-side; no local lookups --------------------------
    def document(self, name: str) -> Any:
        return None

    def reference(self, name: str) -> Any:
        return None

    # -- lifecycle (close/_ensure_loop come from EngineCore) -----------------
    async def aclose(self) -> None:
        if self._http is not None:
            await self._http.aclose()

    def __repr__(self) -> str:
        return f"RemoteWebClientCore({self._base!r})"


def RemoteWebClient(base_url: str, *, token: str | None = None,
                    transport: Any = None, timeout: float = 60.0) -> Any:
    """Convenience: a sync ``WebClient`` over a ``RemoteWebClientCore``."""
    from .engine import WebClient
    return WebClient(core=RemoteWebClientCore(
        base_url, token=token, transport=transport, timeout=timeout))
