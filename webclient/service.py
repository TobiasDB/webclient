"""The WebClient behind an HTTP API -- browser as a service.

Every operation is one Plan POSTed to ``/execute`` with either a ``url`` (root
a fetch) or a ``document_id`` (continue from a server-side document). A returned
Document crosses the wire as a handle ``{"__doc__": {...}}`` whose content is
reached only by a further plan rooted at that handle's id; scalars/rows return
inline. Sessions have their own small REST surface; live events stream over a
``/events`` websocket. The plan is the same wire form ``Plan.model_dump()`` the
client records, validated (``from_plan``) before it runs.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from typing import Any, cast

from fastapi import FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

from .errors import WebError, WebException
from .query.expr import from_plan
from .interface import Document, Reference, WebClient
from .settings import current as _settings

log = logging.getLogger(__name__)


class _DocStore(OrderedDict[str, Any]):
    """An LRU-capped id->Document store, so a long-lived server does not retain
    every document it ever returned. Evicting a stale id just means a follow-up
    plan rooted at it gets a 404 (the client re-fetches)."""

    def __init__(self, cap: int) -> None:
        super().__init__()
        self._cap = cap

    def __setitem__(self, key: str, value: Any) -> None:
        """Store a document, marking it most-recently-used and evicting the oldest past the cap."""
        super().__setitem__(key, value)
        self.move_to_end(key)
        while len(self) > self._cap:
            self.popitem(last=False)  # evict least-recently-used

    def __getitem__(self, key: str) -> Any:
        """Fetch a document, touching it as most-recently-used (raises ``KeyError`` if evicted)."""
        value = super().__getitem__(key)
        self.move_to_end(key)  # LRU touch on access
        return value


def _serialize(value: Any, store: dict[str, Any]) -> Any:
    """A Document -> a stored handle; a Reference -> its url; a Field -> its
    value; a Collection/list/dict recurse; scalars pass through."""
    from .query.collection import Collection, Field

    if isinstance(value, Field):
        return value.get()
    if isinstance(value, Collection):
        return [_serialize(v, store) for v in value]
    if isinstance(value, Document):
        store[value.name] = value
        handle = {"id": value.name, "kind": value.kind, "ok": value.ok}
        return {"__doc__": handle}
    if isinstance(value, Reference):  # rebuilt client-side as a real Reference
        return {"__ref__": value.model_dump(mode="json")}
    from pydantic import BaseModel

    if isinstance(value, BaseModel):  # a value model (Summary/SearchResult/Element/…)
        # tag it so the remote client rebuilds the real model, not a bare dict --
        # remote and local collect() then return the same type.
        return {"__model__": type(value).__name__, "data": value.model_dump(mode="json")}
    if isinstance(value, list):
        return [_serialize(v, store) for v in value]
    if isinstance(value, dict):
        return {k: _serialize(v, store) for k, v in value.items()}
    return value


_SESSION_HINT = "the session expired or was closed; create a new one (POST /sessions)"


def _error(
    http_status: int,
    type_: str,
    message: str,
    *,
    retriable: bool = False,
    hint: str | None = None,
    status_code: int | None = None,
    error: "WebError | None" = None,
) -> JSONResponse:
    """A structured, agent-actionable error body: an autonomous caller branches on
    ``type`` / ``code`` / ``retriable`` / ``remedy`` and follows ``hint`` instead of
    parsing a string. The body is the :class:`WebError` wire shape (so the remote client
    rebuilds the same object), enriched from the error catalogue by ``type`` when no
    ``error`` is given. The inner ``status_code`` defaults to the HTTP status but carries
    the *upstream* status for a proxied fetch failure (a 502 wrapping an origin 500)."""
    from .errors import CATALOG

    if error is None:
        spec = next((s for s in CATALOG.values() if s.type == type_), None)
        error = WebError(
            type=type_, message=message, retriable=retriable,
            status_code=http_status if status_code is None else status_code,
            code=spec.code if spec else "", title=spec.title if spec else "",
            remedy=spec.remedy if spec else None,
            hint=hint if hint is not None else (spec.hint if spec else ""),
        )
    else:
        error = error.model_copy(update={
            "message": message or error.message,
            "hint": hint if hint is not None else error.hint,
            "status_code": error.status_code or (http_status if status_code is None else status_code),
        })
    body = error.model_dump(exclude_defaults=True, exclude_none=True)
    body.setdefault("type", error.type)
    body.setdefault("message", error.message)
    body.setdefault("status_code", error.status_code)
    body.setdefault("retriable", error.retriable)
    return JSONResponse(status_code=http_status, content={"error": body})


def create_app(
    wc: WebClient | None = None,
    token: str | None = None,
    max_docs: int | None = None,
    max_sessions: int | None = None,
    max_session_docs: int | None = None,
    block_private_hosts: bool = False,
) -> FastAPI:
    """A FastAPI app exposing a WebClient over ``/execute`` (Bearer-token
    authorised when ``token`` is set). An existing client may be supplied;
    ``max_docs`` caps the shared LRU document store, ``max_sessions`` bounds the
    live-session store (expired/closed sessions are reclaimed first),
    ``max_session_docs`` caps EACH session's own document store (its per-session
    resource policy -- the handles a session may hold), and ``block_private_hosts``
    turns on the SSRF guard for a hosted server (refuses plans that resolve to
    loopback/private hosts)."""
    caps = _settings().service
    max_docs = caps.max_docs if max_docs is None else max_docs
    max_sessions = caps.max_sessions if max_sessions is None else max_sessions
    max_session_docs = caps.max_session_docs if max_session_docs is None else max_session_docs
    log.info("service: max_docs=%d max_sessions=%d max_session_docs=%d ssrf_guard=%s",
             max_docs, max_sessions, max_session_docs, block_private_hosts)
    app = FastAPI()
    app.state.wc = (
        wc if wc is not None else WebClient(block_private_hosts=block_private_hosts)
    )
    app.state.docs = _DocStore(max_docs)  # the shared client's handles
    app.state.sessions = {}
    #: each server-side session's OWN document store (2b: a session's held handles are
    #: bounded and disposed with it, not leaked into the shared LRU). Keyed by session id.
    app.state.session_docs = {}

    def _auth(authorization: str | None) -> None:
        if token is not None and authorization != f"Bearer {token}":
            raise HTTPException(status_code=401, detail="bad token")

    def _store_for(sid: str | None) -> _DocStore:
        """The document store a plan's handles live in: the session's own bounded store
        when it is session-scoped (so ``document_id`` handles resolve within -- and are
        reclaimed with -- that session), else the shared client store."""
        store = app.state.session_docs.get(sid) if sid else None
        return cast(_DocStore, store) if store is not None else app.state.docs

    def _drop_session(sid: str) -> None:
        """Reclaim a session and its per-session document store."""
        app.state.sessions.pop(sid, None)
        app.state.session_docs.pop(sid, None)

    def _sweep_sessions() -> None:
        """Drop closed or past-ttl sessions (and their stores) so nothing leaks them."""
        now = time.time()
        for sid, s in list(app.state.sessions.items()):
            expired = s.expires_at is not None and now > s.expires_at
            if s.status != "running" or expired:
                _drop_session(sid)

    @app.get("/health", response_model=None)
    def health() -> "dict[str, Any]":
        """Liveness + a resource snapshot (no auth): the pool's lease counts, the document
        and session store sizes -- what a load balancer / autoscaler polls."""
        wc_: WebClient = app.state.wc
        pool = wc_._the_engine().pool
        stats = pool.stats().model_dump() if pool is not None else {}
        return {
            "ok": True,
            "pool": stats,
            "docs": len(app.state.docs),
            "sessions": len(app.state.sessions),
        }

    @app.post("/execute", response_model=None)
    def execute(
        body: dict[str, Any], authorization: str | None = Header(default=None)
    ) -> "dict[str, Any] | JSONResponse":
        _auth(authorization)
        wc_: WebClient = app.state.wc
        try:
            expr = from_plan(body["plan"], wc_)
        except ValueError as exc:  # unknown root / private name / unknown op
            return _error(
                422,
                "InvalidPlan",
                str(exc),
                hint="check the plan's root, operator and step names; names "
                "starting with '_' and unknown roots/operators are refused before "
                "dispatch",
            )
        sid = expr._plan.session_id
        if log.isEnabledFor(logging.DEBUG):
            log.debug("execute session=%s plan=%s", sid, expr._plan.describe())
        # a session-scoped plan runs on the server-side session (its identity /
        # cookies) and its handles live in that session's own store, else on the
        # shared client + shared store.
        engine = app.state.sessions[sid] if sid in app.state.sessions else wc_
        store = _store_for(sid)
        if "document_id" in body:
            if body["document_id"] not in store:
                return _error(
                    404,
                    "NoSuchDocument",
                    f"no server-side document {body['document_id']!r}",
                    retriable=True,  # 2d: the handle was evicted -- re-run its plan
                    hint="the document handle expired from the store (or its session "
                    "closed); it is stateless to reproduce -- re-run the plan that "
                    "produced it to get a fresh handle, then retry",
                )
            context: Any = store[body["document_id"]]
        elif "context_plan" in body:  # a client ref/fetch context plan
            try:
                context = from_plan(body["context_plan"], wc_)
            except ValueError as exc:
                return _error(
                    422, "InvalidPlan", str(exc), hint="the context_plan is malformed"
                )
        elif "url" in body:
            context = engine.ref(body["url"])
        else:
            context = None
        try:
            result = engine.execute(expr, context)  # the realization machinery
        except WebException as exc:  # a fetch/resolve failure -> structured error
            err = exc.error
            return _error(
                502,
                err.type,
                str(exc),
                retriable=err.retriable,
                status_code=err.status_code,
                hint="retry if retriable; otherwise the target is unavailable, "
                "blocking, or refused by policy",
                error=err,
            )
        return {"rows": _serialize(result, store)}

    @app.get("/document/{doc_id}", response_model=None)
    def document(
        doc_id: str, authorization: str | None = Header(default=None)
    ) -> "dict[str, Any] | JSONResponse":
        _auth(authorization)
        if doc_id not in app.state.docs:
            return _error(
                404,
                "NoSuchDocument",
                f"no server-side document {doc_id!r}",
                retriable=True,  # 2d: evicted -- re-run the plan that produced it
                hint="the document handle expired from the store; re-run the plan "
                "that produced it to get a fresh handle",
            )
        d = app.state.docs[doc_id]
        return {"id": d.name, "kind": d.kind, "ok": d.ok}

    # -- sessions ------------------------------------------------------------
    @app.post("/sessions", response_model=None)
    def create_session(
        body: dict[str, Any], authorization: str | None = Header(default=None)
    ) -> "dict[str, Any] | JSONResponse":
        _auth(authorization)
        _sweep_sessions()  # reclaim expired/closed before enforcing the cap
        if len(app.state.sessions) >= max_sessions:
            return _error(
                429,
                "TooManySessions",
                "the session store is at capacity",
                retriable=True,
                hint="close an existing session (DELETE /sessions/{id}) or retry "
                "after in-flight sessions expire",
            )
        session = app.state.wc.session(ttl=body.get("ttl"))
        app.state.sessions[session.id] = session
        log.info("session opened %s ttl=%s (%d live)", session.id, body.get("ttl"), len(app.state.sessions))
        app.state.session_docs[session.id] = _DocStore(max_session_docs)  # its own bounded store
        return {"id": session.id, "status": session.status}

    @app.get("/sessions/{sid}", response_model=None)
    def get_session(
        sid: str, authorization: str | None = Header(default=None)
    ) -> "dict[str, Any] | JSONResponse":
        _auth(authorization)
        if sid not in app.state.sessions:
            return _error(
                404, "NoSuchSession", f"no session {sid!r}", hint=_SESSION_HINT
            )
        return {"id": sid, "status": app.state.sessions[sid].status}

    @app.delete("/sessions/{sid}", response_model=None)
    def close_session(
        sid: str, authorization: str | None = Header(default=None)
    ) -> "dict[str, Any] | JSONResponse":
        _auth(authorization)
        if sid not in app.state.sessions:
            return _error(
                404, "NoSuchSession", f"no session {sid!r}", hint=_SESSION_HINT
            )
        app.state.sessions[sid].close()
        _drop_session(sid)  # dispose the session AND its per-session document store
        log.info("session closed %s", sid)
        return {"id": sid, "status": "closed"}

    # -- tools: the registry, mounted (never hand-written per verb) -------------
    from .tools import TOOLS, ToolError

    def _tool_client(body: dict[str, Any]) -> "Any":
        """The client a tool runs on: a named server-side ``session`` (its identity /
        cookies) or the shared client. A JSONResponse error if the session is unknown."""
        sid = body.get("session")
        if sid is None:
            return app.state.wc
        if sid not in app.state.sessions:
            return _error(404, "NoSuchSession", f"no session {sid!r}", hint=_SESSION_HINT)
        return app.state.sessions[sid]

    def _run_tool(name: str, body: dict[str, Any]) -> "Any":
        """Validate + run one registered tool, mapping bad arguments to a 422 and a
        fetch / resolve failure to a structured 502."""
        from .tools import dispatch as _dispatch

        client = _tool_client(body)
        if isinstance(client, JSONResponse):
            return client
        try:
            return _dispatch(name, body, client)
        except ToolError as exc:
            return _error(
                422, "InvalidRequest", str(exc),
                hint=f"POST the tool's arguments as JSON; see GET /tools for {name!r}'s schema",
            )
        except WebException as exc:
            return _error(
                502, exc.error.type, str(exc), retriable=exc.error.retriable,
                status_code=exc.error.status_code, error=exc.error,
                hint="retry if retriable; else the target is unavailable or blocked",
            )

    @app.get("/tools", response_model=None)
    def tools(authorization: str | None = Header(default=None)) -> "list[dict[str, Any]] | JSONResponse":
        """Every tool: name, description, input JSON Schema, what it returns, its story."""
        _auth(authorization)
        from .tools import schema as _schema

        return _schema()

    @app.post("/tools/{name}", response_model=None)
    def run_tool(
        name: str, body: dict[str, Any], authorization: str | None = Header(default=None)
    ) -> "dict[str, Any] | JSONResponse":
        """Run a registered tool; the result comes back as ``{"result": ...}``."""
        _auth(authorization)
        if name not in TOOLS:
            return _error(404, "InvalidRequest", f"no tool {name!r}", hint="GET /tools lists them")
        out = _run_tool(name, body)
        return out if isinstance(out, JSONResponse) else {"result": out}

    def _mount_alias(path: str, tool_name: str, *, bare: bool) -> None:
        """A legacy verb path (``/markdown``, ``/crawl``, ...) as an alias of its tool; the
        crawl alias returns the tool's dict bare (its historical shape), the rest ``{"result"}``."""
        def handler(body: dict[str, Any], authorization: str | None = Header(default=None)) -> "Any":
            _auth(authorization)
            out = _run_tool(tool_name, body)
            if isinstance(out, JSONResponse) or bare:
                return out
            return {"result": out}

        handler.__name__ = f"alias_{tool_name}"
        app.post(path, response_model=None)(handler)

    for _t in TOOLS.values():
        for _alias in _t.aliases:
            _mount_alias(f"/{_alias}", _t.name, bare=_t.name == "crawl")

    # -- plan authoring: validate / pretty-print / (de)serialise a lazy expr --
    @app.post("/plan", response_model=None)
    def plan(
        body: dict[str, Any], authorization: str | None = Header(default=None)
    ) -> "dict[str, Any] | JSONResponse":
        """Author-time helper for a lazy expression: accepts a ``plan`` (dict) or a
        ``blob`` (a ``to_blob`` string), validates it (the wire safety boundary),
        and returns its human-readable ``describe`` plus the round-tripped ``plan``
        and ``blob`` -- so an LLM can write a plan, check it validates and reads as
        intended, and get the token-cheap blob. With ``"run": true`` it also
        executes it (``url``/``document_id`` supply the context, as for /execute)."""
        _auth(authorization)
        wc_ = app.state.wc
        source = body.get("blob") if body.get("blob") is not None else body.get("plan")
        if source is None:
            return _error(
                422, "InvalidRequest", "provide a 'plan' (object) or a 'blob' (string)",
                hint='POST {"plan": {...}} or {"blob": "<json>"}; add "run": true to run',
            )
        try:
            expr = from_plan(source, wc_)
        except ValueError as exc:
            return _error(
                422, "InvalidPlan", str(exc),
                hint="check the root/operator/step names and the blob encoding",
            )
        out: dict[str, Any] = {
            "valid": True,
            "describe": expr._plan.describe(),
            "plan": expr._plan.model_dump(mode="json"),
            "blob": expr.to_blob(),
        }
        if body.get("run"):
            if body.get("document_id") in app.state.docs:
                context: Any = app.state.docs[body["document_id"]]
            elif body.get("url"):
                context = wc_.ref(body["url"])
            else:
                context = None
            try:
                result = wc_.execute(expr, context)
            except WebException as exc:
                return _error(
                    502, exc.error.type, str(exc), retriable=exc.error.retriable,
                    status_code=exc.error.status_code,
                    hint="retry if retriable; else the target is unavailable or blocked",
                    error=exc.error,
                )
            out["rows"] = _serialize(result, app.state.docs)
        return out

    # -- live event stream ---------------------------------------------------
    def _wire(event: Any) -> dict[str, Any]:
        """An event as JSON for the socket: byte payloads are dropped (a snapshot's content
        is not streamed; the trace holds it) and a reference is rendered as its url."""
        data = event.model_dump(mode="python", exclude_none=True)
        data.pop("content", None)
        data.pop("body", None)
        req = data.pop("request", None)
        if req is not None:
            try:
                data["url"] = str(event.request.dispatch("url"))
            except Exception:  # noqa: BLE001
                pass
        if "error" in data and hasattr(event, "error"):
            data["error"] = event.error.model_dump(mode="json", exclude_none=True)
        import json as _json

        return cast("dict[str, Any]", _json.loads(_json.dumps(data, default=str)))

    @app.websocket("/events")
    async def events(ws: WebSocket) -> None:
        """The live event stream. ``?topic=`` filters by dotted prefix; ``?since=<n>`` first
        replays the bus's retained history past that global cursor (a reconnecting client
        resumes without a gap, bounded by ``limits.event_history``); ``?trace=<path>`` streams
        a STORED trace instead of the live bus (``?speed=`` scales its original timing, ``0``
        = as fast as possible) -- so live and replay share one wire."""
        await ws.accept()
        topic = ws.query_params.get("topic", "")
        trace_path = ws.query_params.get("trace")
        if trace_path:
            from .trace import read as _read_trace

            speed = float(ws.query_params.get("speed", "0") or 0)
            try:
                prev_ts: float | None = None
                for event in _read_trace(trace_path).events:
                    if topic and not event.topic.startswith(topic):
                        continue
                    if speed > 0 and prev_ts is not None and event.ts is not None:
                        await asyncio.sleep(max(0.0, (event.ts - prev_ts) / speed))
                    prev_ts = event.ts
                    await ws.send_json(_wire(event))
                await ws.send_json({"topic": "trace.end"})
            except WebSocketDisconnect:
                pass
            return
        since = int(ws.query_params.get("since", "0") or 0)
        loop = asyncio.get_event_loop()
        queue: asyncio.Queue[Any] = asyncio.Queue()

        def handler(event: Any) -> None:  # engine thread -> server loop
            loop.call_soon_threadsafe(queue.put_nowait, _wire(event))

        bus = app.state.wc.bus
        # subscribe FIRST, then replay history, so no event falls between the two
        sub = bus.subscribe(topic, handler)
        backlog = bus.since(since, topic=topic) if since else []
        try:
            sent = 0
            for event in backlog:
                await ws.send_json(_wire(event))
                sent = event.n or sent
            while True:
                item = await queue.get()
                if sent and (item.get("n") or 0) <= sent:
                    continue  # already delivered from the backlog
                await ws.send_json(item)
        except WebSocketDisconnect:
            pass
        finally:
            sub.cancel()

    return app


def main(argv: "list[str] | None" = None) -> None:  # pragma: no cover - a process entry
    """``python -m webclient.service``: serve :func:`create_app` with uvicorn. Reads
    ``WEBCLIENT_SERVICE_TOKEN`` (bearer auth; unset = open), ``WEBCLIENT_SERVICE_HOST`` /
    ``WEBCLIENT_SERVICE_PORT`` (default 0.0.0.0:8000) and ``WEBCLIENT_SERVICE_SSRF_GUARD``
    (``1`` refuses private hosts), plus the ordinary ``WEBCLIENT_*`` settings."""
    import os

    import uvicorn

    from .settings import current

    current().configure_logging()
    app = create_app(
        token=os.environ.get("WEBCLIENT_SERVICE_TOKEN") or None,
        block_private_hosts=os.environ.get("WEBCLIENT_SERVICE_SSRF_GUARD", "") in ("1", "true", "yes"),
    )
    uvicorn.run(
        app,
        host=os.environ.get("WEBCLIENT_SERVICE_HOST", "0.0.0.0"),
        port=int(os.environ.get("WEBCLIENT_SERVICE_PORT", "8000")),
        log_config=None,
    )


if __name__ == "__main__":  # pragma: no cover
    main()


__all__ = ["create_app", "main"]
