"""The engine's REMOTE transport: the same WebClient in ``"remote"`` dispatch mode.

Remote is not a separate client or a bespoke subtype -- it is the SAME cores whose engine
runs in a third dispatch mode (see ``WebCore._dispatch_mode``). An engine in remote mode
holds a :class:`ServiceTransport` (this module) instead of a local http+browser pool: every
op that needs the server records a Plan onto the core's remote root and ``collect``s it in
one round-trip to a :mod:`webclient.service` app, so a ``WebClient`` over it builds the very
same plans with no local browser or lxml -- only httpx + pydantic. The engine decides how a
result comes back: a fetched document is a real ``Document`` handle (id/kind/ok inline, its
content ops round-trip), a reference a real ``Reference``, a scalar its value. A session's
server-side identity is its ``_server_sid`` -- the transport is shared on the engine, so the
sid travels with the calling session (never on the transport), and sessions nest cleanly.
"""

from __future__ import annotations

from typing import Any, cast

import httpx

from ..query.expr import Expr
from .document import Document
from .reference import Reference


# -- wire (de)serialisation --------------------------------------------------
def url_of(source: dict[str, Any]) -> str:
    """The URL a reference-rooted plan's ``source`` spec resolves to."""
    return cast(str, Reference(**source).dispatch("url"))


def reject_sequence(expr: Any) -> None:
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


def wire_models() -> "dict[str, type[Any]]":
    """Name -> class for the value models an op can return over the wire (built once), so
    :func:`deserialize` rebuilds a real ``Transport``/``Metadata``/… model from a tagged
    ``{"__model__": ...}`` payload."""
    global _WIRE_MODELS_CACHE
    if _WIRE_MODELS_CACHE is None:
        from ..models import (
            CORE_EVENTS,
            Event,
        )
        from .client.models import Robots
        from .crawl.models import Edge
        from .document.models import (
            Element,
            Flag,
            Metadata,
            PageCard,
            Signal,
            Structure,
            Transport,
        )

        models: list[type[Any]] = [
            Transport, Metadata, Structure, Signal, Flag, Element, PageCard,
            Edge, Robots, Event, *CORE_EVENTS,
        ]
        _WIRE_MODELS_CACHE = {m.__name__: m for m in models}
    return _WIRE_MODELS_CACHE


def doc_handle(client: Any, meta: dict[str, Any]) -> Document:
    """A server-side document as a real ``Document`` bound to ``client``: id/kind/ok are
    inline (``status_code`` set so ``ok`` agrees); content ops round-trip (``_remote_handle``)."""
    doc = Document(
        url="",
        kind=meta.get("kind", "html"),
        status_code=200 if meta.get("ok", True) else 502,
    )
    doc.id = doc.name = meta["id"]
    doc._client = client
    doc._remote_handle = True
    return doc


def deserialize(client: Any, rows: Any) -> Any:
    """Rebuild real cores/values from the service's clean JSON, bound to ``client`` -- a
    ``{"__doc__"}`` handle, a ``{"__ref__"}`` reference, a ``{"__model__"}`` value model,
    or a list thereof; anything else passes through."""
    if isinstance(rows, dict) and "__doc__" in rows:
        return doc_handle(client, rows["__doc__"])
    if isinstance(rows, dict) and "__ref__" in rows:
        ref = Reference(**rows["__ref__"])
        ref._client = client
        return ref
    if isinstance(rows, dict) and "__model__" in rows:
        # rebuild the real value model (Transport/Metadata/…) so a remote result has the
        # same type as a local one (s.title, not s["title"]).
        model = wire_models().get(rows["__model__"])
        data = rows.get("data", {})
        return model.model_validate(data) if model is not None else data
    if isinstance(rows, list):
        return [deserialize(client, r) for r in rows]
    return rows


def _raise_for_body(resp: Any) -> None:
    """Turn a non-2xx service response into a structured ``RemoteError``."""
    if 200 <= resp.status_code < 300:
        return
    from ..errors import RemoteError, WebError

    err: WebError | None = None
    try:  # the service sends {"error": {type, message, status_code, ...}}
        payload = resp.json()
        if isinstance(payload, dict) and isinstance(payload.get("error"), dict):
            err = WebError(**payload["error"])
    except Exception:
        pass
    raise RemoteError(resp.status_code, resp.text[:200], error=err)


# -- the transport an engine holds in remote mode ----------------------------
class ServiceTransport:
    """The HTTP transport an :class:`~.engine.Engine` holds in ``"remote"`` dispatch mode:
    the connection to a :mod:`webclient.service` app plus the plan-POST + server-session
    drivers. Owned by the ENGINE (like the local pool), SHARED by every session scoped on
    it -- so a session's server identity travels as the calling client's ``_server_sid``,
    not on the transport. The engine's ``execute`` routes here when it is set."""

    def __init__(self, url: str, token: str | None, timeout: float, retry: Any = None) -> None:
        from ..policy import RetryPolicy

        self.url = url.rstrip("/")
        self.token = token
        self.http = httpx.Client(timeout=timeout)
        #: the RESILIENCY policy for the service connection itself (roadmap N17): a
        #: transport error or a 429 / 5xx from the service is retried per ``retry``.
        self.retry: Any = retry if retry is not None else RetryPolicy()

    def _post(self, path: str, body: dict[str, Any]) -> Any:
        """POST to the service with the retry policy: transport errors and retriable
        statuses are re-sent with backoff (honouring Retry-After); the last response is
        returned for the caller to interpret."""
        import time as _time

        attempt = 0
        while True:
            try:
                resp = self.http.post(f"{self.url}{path}", json=body, headers=self._headers())
            except httpx.TransportError:
                if not self.retry.on_transport or attempt >= self.retry.max:
                    raise
                resp = None
            if resp is not None and resp.status_code not in self.retry.on_statuses:
                return resp
            if attempt >= self.retry.max:
                return resp if resp is not None else self.http.post(f"{self.url}{path}", json=body, headers=self._headers())
            delay = self.retry.base * (2 ** attempt) if self.retry.backoff == "exp" else self.retry.base
            if resp is not None and self.retry.respect_retry_after:
                after = resp.headers.get("retry-after")
                if after and after.isdigit():
                    delay = min(float(after), 60.0)
            _time.sleep(delay)
            attempt += 1

    def _headers(self) -> dict[str, str]:
        """The bearer-auth header for a token-protected service (empty when no token)."""
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    # -- execution: one Plan POSTed to /execute ------------------------------
    def execute(self, client: Any, expr: Any, context: Any = None, *, stream: bool = False) -> Any:
        """Run a plan on the service: POST it to ``/execute`` (threading the calling session's
        server ``_server_sid`` and any context), then rebuild the response into real cores (a
        ``Document`` handle / a ``Reference``) or a value, so a remote ``collect()`` matches a
        local one. Each fresh handle is stamped with its producing plan for eviction-replay."""
        reject_sequence(expr)
        sid = getattr(client, "_server_sid", "")  # the calling session's server identity
        if sid:  # a session-scoped plan resolves through that server session
            expr = Expr(expr._plan.model_copy(update={"session_id": sid}), expr._client)
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
        resp = self._post("/execute", body)
        _raise_for_body(resp)
        from .client import _materialize

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

    # -- server-side sessions ------------------------------------------------
    def open_session(self, ttl: float | None) -> str:
        """Create a server-side session and return its id (stored on the child session as
        ``_server_sid`` and threaded into its plans by :meth:`execute`)."""
        resp = self._post("/sessions", {"ttl": ttl})
        resp.raise_for_status()
        return cast(str, resp.json()["id"])

    def close_session(self, sid: str) -> None:
        """Delete the server-side session ``sid`` (best-effort -- a dispose never raises)."""
        try:
            self.http.delete(f"{self.url}/sessions/{sid}", headers=self._headers())
        except Exception:  # noqa: BLE001 - best-effort dispose
            pass

    def close(self) -> None:
        """Close the HTTP connection to the service."""
        self.http.close()


__all__ = [
    "ServiceTransport", "deserialize", "reject_sequence", "url_of", "wire_models",
    "doc_handle",
]
