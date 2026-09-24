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
from pathlib import Path
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
        handle = {"id": value.name, "kind": value.kind, "ok": value.ok, "url": value.final_url or value.url,
                  "title": value.title, "live": value._page is not None}
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


def op_catalogue() -> "dict[str, Any]":
    """Build the op catalogue (see ``GET /ops``) from the cores' backing tables."""
    import inspect

    from .core.document import Document as _Doc
    from .core.reference import Reference as _Ref

    def params_of(fn: Any) -> "list[dict[str, Any]]":
        out: list[dict[str, Any]] = []
        try:
            sig = inspect.signature(fn)
        except (TypeError, ValueError):
            return out
        for name, p in sig.parameters.items():
            if name in ("self", "core") or p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD):
                if p.kind is p.VAR_KEYWORD:
                    out.append({"name": f"**{name}", "required": False, "kind": "keyword"})
                continue
            required = p.default is inspect.Parameter.empty
            out.append({
                "name": name, "required": required,
                "kind": "keyword" if p.kind is p.KEYWORD_ONLY else "positional",
                "default": None if required else (p.default if isinstance(p.default, (str, int, float, bool)) or p.default is None else repr(p.default)),
                "type": p.annotation if isinstance(p.annotation, str) else (getattr(p.annotation, "__name__", None) if p.annotation is not inspect.Parameter.empty else None),
            })
        return out

    def doc_of(fn: Any) -> str:
        d = inspect.getdoc(fn) or ""
        return d.split("\n\n")[0].replace("\n", " ").strip()

    def core_ops(core: Any) -> "list[dict[str, Any]]":
        rows: list[dict[str, Any]] = []
        coll = core.collection_ops()
        io = core.io_ops()
        for op, backing in sorted(core.ops().items()):
            cands = [b for b in core.BACKINGS if op in getattr(b, "provides", ())] or [backing]
            best = max(cands, key=lambda b: len(params_of(getattr(type(b), op, None) or (lambda: None))))
            fn = getattr(type(best), op, None)
            rows.append({"name": op, "kind": "call", "io": op in io, "collection": op in coll,
                         "params": params_of(fn) if fn else [], "doc": doc_of(fn) if fn else ""})
        for op, backing in sorted(core.prop_ops().items()):
            fn = getattr(type(backing), op, None)
            rows.append({"name": op, "kind": "prop", "io": False, "collection": False, "params": [], "doc": doc_of(fn) if fn else ""})
        return rows

    doc_rows = core_ops(_Doc)
    # the hand-written chain ops (bound / lifted): not backing ops, but part of the surface
    doc_rows += [
        {"name": "extract", "kind": "call", "io": False, "collection": False, "bound": True,
         "params": [{"name": "**fields", "required": False, "kind": "keyword"}],
         "doc": "Capture named fields: each keyword is a sub-plan rooted at this element (a row per element under select_all)."},
        {"name": "project", "kind": "call", "io": False, "collection": False, "params": [], "doc": "The captured rows as plain dicts."},
        {"name": "paginate", "kind": "call", "io": True, "collection": True, "bound": True,
         "params": params_of(_Doc.apaginate), "doc": doc_of(_Doc.apaginate)},
        {"name": "limit", "kind": "call", "io": False, "collection": True, "params": [{"name": "n", "required": True, "kind": "positional", "type": "int"}], "doc": "The first n of a collection."},
        {"name": "count", "kind": "prop", "io": False, "collection": False, "params": [], "doc": "How many items a collection holds."},
    ]
    return {"Document": doc_rows, "Reference": core_ops(_Ref)}


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
    traces_dir: "str | Path | None" = None,
    cors_origins: "list[str] | None" = None,
) -> FastAPI:
    """A FastAPI app exposing a WebClient over ``/execute`` (Bearer-token
    authorised when ``token`` is set). An existing client may be supplied;
    ``max_docs`` caps the shared LRU document store, ``max_sessions`` bounds the
    live-session store (expired/closed sessions are reclaimed first),
    ``max_session_docs`` caps EACH session's own document store (its per-session
    resource policy -- the handles a session may hold), and ``block_private_hosts``
    turns on the SSRF guard for a hosted server (refuses plans that resolve to
    loopback/private hosts). ``traces_dir`` is where stored traces are read from (``/traces``); default
    ``./traces`` (or ``WEBCLIENT_TRACES_DIR``). ``cors_origins`` lets browser front ends
    (the separate webclient-ui repository) call this API; default: the local dev servers."""
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
    import os

    app.state.traces_dir = Path(traces_dir or os.environ.get("WEBCLIENT_TRACES_DIR") or "traces")

    def _auth(authorization: str | None) -> None:
        if token is not None and authorization != f"Bearer {token}":
            raise HTTPException(status_code=401, detail="bad token")

    def _store_for(sid: str | None) -> _DocStore:
        """The document store a plan's handles live in: the session's own bounded store
        when it is session-scoped (so ``document_id`` handles resolve within -- and are
        reclaimed with -- that session), else the shared client store."""
        store = app.state.session_docs.get(sid) if sid else None
        return cast(_DocStore, store) if store is not None else app.state.docs

    app.state.recording = set()  # the sessions opened with record=True (the DOM recorder's holders)
    app.state.crawls = {}  # the crawls sessions hold (see /sessions/{sid}/crawls)

    def _release_pages(sid: str) -> int:
        """Release every live browser page the session's documents hold (the captures stay)."""
        session = app.state.sessions.get(sid)
        store = app.state.session_docs.get(sid)
        n = 0
        if session is None or store is None:
            return 0
        for doc in list(store.values()):
            if getattr(doc, "_page", None) is not None:
                try:
                    session.release(doc)
                    n += 1
                except Exception:  # noqa: BLE001 - already gone
                    pass
        return n

    def _drop_session(sid: str) -> None:
        """Reclaim a session: release its pages, close its crawls, drop its stores."""
        _release_pages(sid)
        for cid, held in list(getattr(app.state, "crawls", {}).items()):
            if held.get("session") == sid:
                try:
                    held["crawl"].close()
                except Exception:  # noqa: BLE001
                    pass
                app.state.crawls.pop(cid, None)
        if sid in app.state.recording:
            app.state.recording.discard(sid)
            app.state.wc._the_engine().dom_recorders = max(0, app.state.wc._the_engine().dom_recorders - 1)
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
            "resources": wc_.resources(),
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
        if body.get("record"):  # a LIVE session: the rrweb recorder rides every browser page
            # opened while it is open, so a UI can replay the page as it changes
            app.state.wc._the_engine().dom_recorders += 1
            app.state.recording.add(session.id)
        app.state.sessions[session.id] = session
        log.info("session opened %s ttl=%s (%d live)", session.id, body.get("ttl"), len(app.state.sessions))
        app.state.session_docs[session.id] = _DocStore(max_session_docs)  # its own bounded store
        return {"id": session.id, "status": session.status}

    def _touch(sid: str) -> None:
        """A read of the session is activity: push its expiry out by its ttl again."""
        s = app.state.sessions.get(sid)
        if s is not None and s.ttl is not None:
            s.expires_at = time.time() + s.ttl

    def _session_info(sid: str) -> dict[str, Any]:
        s = app.state.sessions[sid]
        store = app.state.session_docs.get(sid) or {}
        return {"id": sid, "status": s.status, "ttl": s.ttl, "expires_at": s.expires_at,
                "documents": len(store), "live_pages": sum(1 for d in store.values() if getattr(d, "_page", None) is not None),
                "crawls": sum(1 for h in app.state.crawls.values() if h.get("session") == sid),
                "recording": sid in app.state.recording}

    @app.get("/sessions", response_model=None)
    def list_sessions(authorization: str | None = Header(default=None)) -> "list[dict[str, Any]] | JSONResponse":
        """Every live session on this service with what it holds -- the view that lets a UI
        find who is holding the browser pages, and close or release them."""
        _auth(authorization)
        _sweep_sessions()
        return [_session_info(sid) for sid in app.state.sessions]

    @app.get("/sessions/{sid}", response_model=None)
    def get_session(
        sid: str, authorization: str | None = Header(default=None)
    ) -> "dict[str, Any] | JSONResponse":
        _auth(authorization)
        if sid not in app.state.sessions:
            return _error(
                404, "NoSuchSession", f"no session {sid!r}", hint=_SESSION_HINT
            )
        _touch(sid)
        return _session_info(sid)

    @app.post("/sessions/{sid}/release", response_model=None)
    def release_session_pages(sid: str, authorization: str | None = Header(default=None)) -> "dict[str, Any] | JSONResponse":
        """Release every live page the session holds (its captures stay usable) -- the way out
        when the page pool is exhausted."""
        _auth(authorization)
        if sid not in app.state.sessions:
            return _error(404, "NoSuchSession", f"no session {sid!r}", hint=_SESSION_HINT)
        return {"id": sid, "released": _release_pages(sid)}

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

    @app.get("/ops", response_model=None)
    def ops(authorization: str | None = Header(default=None)) -> "dict[str, Any] | JSONResponse":
        """The op catalogue: every op each core exposes (the same tables the typed surface is
        generated from), with its parameters, so a UI builds an object's menu from the surface
        itself instead of a hand-written list. Per core: ``ops`` (name, kind call/prop, io,
        collection, params [name, required, default, kind], doc). ``Document`` also lists the
        hand-written chain ops (``extract`` / ``project`` / ``paginate`` / ``limit`` / ``count``)."""
        _auth(authorization)
        return op_catalogue()

    @app.get("/signals", response_model=None)
    def signals(authorization: str | None = Header(default=None)) -> "list[dict[str, Any]] | JSONResponse":
        """The signals catalogue, from the registry: every flag with its remedy (fixed or
        derived) and the detectors feeding it (name, stage, contra, what they need, the
        docstring). What the docs and the website render; never hand-copied."""
        _auth(authorization)
        from .signals.registry import DETECTORS, FLAGS

        out: list[dict[str, Any]] = []
        for name, spec in FLAGS.items():
            dets = [
                {"name": d.name, "stage": d.stage, "contra": d.contra, "needs": list(d.needs),
                 "description": (d.fn.__doc__ or "").strip().split("\n\n")[0]}
                for d in DETECTORS if d.flag == name
            ]
            remedy = spec.remedy if isinstance(spec.remedy, str) else ("derived" if spec.remedy else None)
            out.append({"name": name, "remedy": remedy, "has_value": spec.value is not None, "detectors": dets})
        return out

    @app.get("/errors", response_model=None)
    def errors_catalogue(authorization: str | None = Header(default=None)) -> "list[dict[str, Any]] | JSONResponse":
        """The error catalogue: every code with its type, title, remedy, hint, retriable
        default, status and the longer doc."""
        _auth(authorization)
        from .errors import CATALOG

        return [
            {"code": s.code, "type": s.type, "title": s.title, "remedy": s.remedy, "hint": s.hint,
             "retriable": s.retriable, "status_code": s.status_code, "doc": s.doc}
            for s in CATALOG.values()
        ]

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
        if body.get("wireframe"):  # the plan pictured (a self-contained HTML page)
            from .query.viz import explain, wireframe

            out["wireframe"] = wireframe(expr._plan)
            out["explain"] = explain(expr._plan)
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
    # -- the UI lives in its own repository (webclient-ui) and talks to this API over
    # HTTP: allow it (and any other browser client) via CORS. ``cors_origins`` defaults to the
    # local dev servers; ``["*"]`` opens it up (WEBCLIENT_CORS_ORIGINS, comma-separated).
    from fastapi.middleware.cors import CORSMiddleware

    origins = cors_origins if cors_origins is not None else [
        o.strip() for o in os.environ.get(
            "WEBCLIENT_CORS_ORIGINS",
            "http://localhost:5173,http://127.0.0.1:5173,http://localhost:4321,http://127.0.0.1:4321,"
            "http://localhost:6006,http://127.0.0.1:6006",
        ).split(",") if o.strip()
    ]
    app.add_middleware(CORSMiddleware, allow_origins=origins, allow_methods=["*"], allow_headers=["*"])

    def _trace_file(trace_id: str) -> "Path | JSONResponse":
        """The file of a stored trace by id (``<traces_dir>/<id>.jsonl``); 404 when unknown or
        outside ``traces_dir`` (no path escapes)."""
        base = Path(app.state.traces_dir).resolve()
        target = (base / f"{trace_id}.jsonl").resolve()
        if not str(target).startswith(str(base)) or not target.is_file():
            return _error(404, "InvalidRequest", f"no trace {trace_id!r}", hint="GET /traces lists them")
        return target

    @app.get("/traces", response_model=None)
    def traces(authorization: str | None = Header(default=None)) -> "list[dict[str, Any]] | JSONResponse":
        """The stored traces under ``traces_dir`` (one ``.jsonl`` each): id, event count,
        started / finished."""
        _auth(authorization)
        from .trace import read as _read

        base = Path(app.state.traces_dir)
        out: list[dict[str, Any]] = []
        if base.exists():
            for f in sorted(base.glob("*.jsonl")):
                r = _read(f)
                out.append({"id": f.stem, "events": r.count, "started": r.header.get("started"),
                            "finished": r.footer.get("finished"), "schema_version": r.schema_version})
        return out

    @app.get("/traces/{trace_id}", response_model=None)
    def trace_summary(trace_id: str, authorization: str | None = Header(default=None)) -> "dict[str, Any] | JSONResponse":
        """The trace's header + footer facts: schema, package version, started / finished,
        events, snapshots, documents, whether a plan was recorded."""
        _auth(authorization)
        f = _trace_file(trace_id)
        if isinstance(f, JSONResponse):
            return f
        from .trace import read as _read

        return {"id": trace_id, **_read(f).summary()}

    @app.get("/traces/{trace_id}/events", response_model=None)
    def trace_events(trace_id: str, topic: str = "", authorization: str | None = Header(default=None)) -> "list[dict[str, Any]] | JSONResponse":
        """The stream as JSON (the wire view: byte payloads and rrweb chunk bodies dropped;
        ``/traces/{id}/events/{n}`` has one event in full)."""
        _auth(authorization)
        f = _trace_file(trace_id)
        if isinstance(f, JSONResponse):
            return f
        from .trace import read as _read

        return [_wire(e) for e in _read(f).events if not topic or e.topic.startswith(topic)]

    @app.get("/traces/{trace_id}/events/{n}", response_model=None)
    def trace_event(trace_id: str, n: int, authorization: str | None = Header(default=None)) -> "dict[str, Any] | JSONResponse":
        """One event in full -- a snapshot's content, a response body (text), an rrweb chunk."""
        _auth(authorization)
        f = _trace_file(trace_id)
        if isinstance(f, JSONResponse):
            return f
        from .trace import encode as _encode
        from .trace import read as _read

        for e in _read(f).events:
            if e.n == n:
                return _encode(e)
        return _error(404, "InvalidRequest", f"no event #{n} in trace {trace_id!r}")

    @app.get("/traces/{trace_id}/rrweb", response_model=None)
    def trace_rrweb(trace_id: str, document_id: str | None = None, custom: bool = True,
                    authorization: str | None = Header(default=None)) -> "list[dict[str, Any]] | JSONResponse":
        """The whole stream as rrweb events for one player: the DOM (recorded, or synthesised
        from the snapshots of a static run) plus every other event as an rrweb custom event
        tagged with its topic (``custom=false`` for the DOM alone)."""
        _auth(authorization)
        f = _trace_file(trace_id)
        if isinstance(f, JSONResponse):
            return f
        from .trace import read as _read

        return _read(f).rrweb(document_id, custom=custom)

    @app.get("/traces/{trace_id}/har", response_model=None)
    def trace_har(trace_id: str, authorization: str | None = Header(default=None)) -> "dict[str, Any] | JSONResponse":
        """The trace's network as a HAR (derived from the stream)."""
        _auth(authorization)
        f = _trace_file(trace_id)
        if isinstance(f, JSONResponse):
            return f
        from .trace import read as _read

        return _read(f).har()

    @app.get("/traces/{trace_id}/plan", response_model=None)
    def trace_plan(trace_id: str, authorization: str | None = Header(default=None)) -> "dict[str, Any] | JSONResponse":
        """The Plan that produced the run: its blob and description (404 when none was recorded)."""
        _auth(authorization)
        f = _trace_file(trace_id)
        if isinstance(f, JSONResponse):
            return f
        from .query.expr import from_blob
        from .trace import read as _read

        blob = _read(f).plan_blob
        if blob is None:
            return _error(404, "InvalidRequest", f"trace {trace_id!r} recorded no plan")
        return {"blob": blob, "describe": from_blob(blob, None).describe()}

    @app.get("/events", response_model=None)
    def events_history(since: int = 0, topic: str = "", document_id: str | None = None, payload: bool = False,
                       limit: int = 500, authorization: str | None = Header(default=None)) -> "list[dict[str, Any]] | JSONResponse":
        """The bus's retained history over plain HTTP (the socket's catch-up, pollable):
        events past the ``since`` cursor, by topic prefix and document; ``payload=true``
        includes the byte payloads and the rrweb chunk bodies (what a live DOM replay
        appends to its player)."""
        _auth(authorization)
        from .trace import encode as _encode

        bus = app.state.wc.bus
        out = []
        for e in bus.since(since, topic=topic):
            if document_id and e.document_id != document_id:
                continue
            out.append(_encode(e, payload=payload))
            if len(out) >= limit:
                break
        return out

    # -- the documents a session holds: what the UI shows at the top (static or live), reloadable --
    def _handle(doc: Any, doc_id: "str | None" = None) -> dict[str, Any]:
        tiers = list(getattr(doc, "_tiers", []) or [])
        return {"id": doc_id or doc.name, "url": doc.final_url or doc.url, "title": doc.title, "kind": doc.kind,
                "status_code": doc.status_code, "ok": doc.ok, "live": getattr(doc, "_page", None) is not None,
                "tier": tiers[-1] if tiers else "static", "tiers": tiers}

    def _session_doc(sid: str, doc_id: str) -> "tuple[Any, Any] | JSONResponse":
        if sid not in app.state.sessions:
            return _error(404, "NoSuchSession", f"no session {sid!r}", hint=_SESSION_HINT)
        store = _store_for(sid)
        if doc_id not in store:
            return _error(404, "NoSuchDocument", f"no document {doc_id!r} in session {sid!r}", retriable=True,
                          hint="the handle expired or was closed; open the page again")
        return app.state.sessions[sid], store[doc_id]

    @app.get("/sessions/{sid}/documents", response_model=None)
    def list_documents(sid: str, authorization: str | None = Header(default=None)) -> "list[dict[str, Any]] | JSONResponse":
        """The documents this session holds, newest last: url, title, kind, whether the page is
        LIVE (a browser page held open) or a static capture, and the tier that fetched it."""
        _auth(authorization)
        if sid not in app.state.sessions:
            return _error(404, "NoSuchSession", f"no session {sid!r}", hint=_SESSION_HINT)
        _touch(sid)
        return [_handle(d, k) for k, d in _store_for(sid).items()]

    @app.post("/sessions/{sid}/documents", response_model=None)
    def open_document(sid: str, body: dict[str, Any], authorization: str | None = Header(default=None)) -> "dict[str, Any] | JSONResponse":
        """Open ``url`` in the session: ``browser`` false / "auto" / "always". The document is a
        CAPTURE unless ``live`` is true, in which case the browser page stays held (a pool
        page) so it can be driven -- release it when done. The handle comes back."""
        _auth(authorization)
        if sid not in app.state.sessions:
            return _error(404, "NoSuchSession", f"no session {sid!r}", hint=_SESSION_HINT)
        url = body.get("url")
        if not url:
            return _error(422, "InvalidRequest", "provide 'url'")
        browser = body.get("browser", False)
        live = bool(body.get("live", False)) and bool(browser)
        session = app.state.sessions[sid]
        try:
            doc = session.fetch(url, browser=browser or (True if live else False), keep_alive=live)
        except WebException as exc:
            return _error(502, exc.error.type, str(exc), retriable=exc.error.retriable, status_code=exc.error.status_code,
                          error=exc.error, hint="retry if retriable; else the target is unavailable or blocked")
        if not live and getattr(doc, "_page", None) is not None:
            session.release(doc)  # a CAPTURE: the content is kept, the browser page goes back to the pool
        _store_for(sid)[doc.name] = doc
        return _handle(doc)

    @app.get("/sessions/{sid}/documents/{doc_id}/views", response_model=None)
    def document_views(sid: str, doc_id: str, include: str = "card", authorization: str | None = Header(default=None)) -> "dict[str, Any] | JSONResponse":
        """The views of a held document, no refetch: ``include`` is a comma list of card,
        content, rrweb, patterns, records, flags, skeleton, markdown, controls, elements, transport."""
        _auth(authorization)
        got = _session_doc(sid, doc_id)
        if isinstance(got, JSONResponse):
            return got
        from .tools import views as _views

        return _views(got[1], [v.strip() for v in include.split(",") if v.strip()])

    @app.post("/sessions/{sid}/documents/{doc_id}/reload", response_model=None)
    def reload_document(sid: str, doc_id: str, authorization: str | None = Header(default=None)) -> "dict[str, Any] | JSONResponse":
        """Reload a held document: a live page navigates to its url again; a static capture is
        fetched again with the same tier -- under the SAME id, so a UI keeps its place."""
        _auth(authorization)
        got = _session_doc(sid, doc_id)
        if isinstance(got, JSONResponse):
            return got
        session, doc = got
        try:
            if getattr(doc, "_page", None) is not None:
                doc.goto(doc.final_url or doc.url)
                fresh = doc
            else:
                tiers = list(getattr(doc, "_tiers", []) or [])
                fresh = session.fetch(doc.url, browser=("always" if tiers and tiers[-1] == "browser" else False))
                if getattr(fresh, "_page", None) is not None:
                    session.release(fresh)  # still a capture
        except WebException as exc:
            return _error(502, exc.error.type, str(exc), retriable=exc.error.retriable, status_code=exc.error.status_code, error=exc.error)
        _store_for(sid)[doc_id] = fresh
        return _handle(fresh, doc_id)

    @app.delete("/sessions/{sid}/documents/{doc_id}", response_model=None)
    def close_document(sid: str, doc_id: str, authorization: str | None = Header(default=None)) -> "dict[str, Any] | JSONResponse":
        """Drop a held document (a live page is released)."""
        _auth(authorization)
        got = _session_doc(sid, doc_id)
        if isinstance(got, JSONResponse):
            return got
        session, doc = got
        if getattr(doc, "_page", None) is not None:
            try:
                session.release(doc)
            except Exception:  # noqa: BLE001 - already gone
                pass
        _store_for(sid).pop(doc_id, None)
        return {"id": doc_id, "status": "closed"}

    # -- crawls held by a session: start (auto, manual or with a goal), watch, step, resume --

    def _crawl_state(cid: str) -> dict[str, Any]:
        held = app.state.crawls[cid]
        crawl = held["crawl"]
        pending = getattr(crawl, "pending", None)
        goal = held.get("result")
        pages = []
        for pg in list(crawl.pages):
            pages.append({"url": getattr(pg, "final_url", None) or getattr(pg, "url", ""), "title": getattr(pg, "title", None),
                          "kind": getattr(pg, "kind", None), "status_code": getattr(pg, "status_code", None)})
        return {
            "id": cid, "session": held["session"], "mode": held["mode"], "seeds": held["seeds"],
            "running": bool(held.get("thread") and held["thread"].is_alive()), "done": bool(getattr(crawl, "done", False)),
            "status": getattr(crawl, "status", "running"), "round": int(getattr(crawl, "_round", 0) or 0),
            "pages": pages,
            "frontier": [e.model_dump(mode="json") for e in list(crawl.frontier)[:200]],
            "failures": [f.model_dump(mode="json") for f in list(crawl.failures)],
            "pending": pending.model_dump(mode="json") if pending is not None else None,
            "goal": held.get("goal"),
            "result": ({"reason": goal.reason, "rounds": goal.rounds, "pages": goal.pages,
                        "found": [getattr(pg, "final_url", None) or getattr(pg, "url", "") for pg in goal.found]} if goal is not None else None),
            "error": held.get("error"),
        }

    def _run_in_thread(cid: str, fn: Any) -> None:
        import threading

        held = app.state.crawls[cid]

        def go() -> None:
            try:
                out = fn()
                if out is not None:
                    held["result"] = out
            except Exception as exc:  # noqa: BLE001 - surfaced on the state, never lost
                held["error"] = f"{type(exc).__name__}: {exc}"
                log.warning("crawl %s failed: %s", cid, exc)
        t = threading.Thread(target=go, name=f"crawl-{cid}", daemon=True)
        held["thread"] = t
        t.start()

    @app.post("/sessions/{sid}/crawls", response_model=None)
    def start_crawl(sid: str, body: dict[str, Any], authorization: str | None = Header(default=None)) -> "dict[str, Any] | JSONResponse":
        """Start a crawl in a session: ``seeds`` (a URL or a list), the budget (``max_pages``,
        ``width``, ``depth``), ``keywords`` (goal words that steer the frontier), ``obey_robots``,
        ``browser``, and the ``mode``: ``"auto"`` runs to completion in the background;
        ``"manual"`` fetches nothing until you ``step`` it with your picks. A ``goal``
        (``{"title_contains": ...}`` / ``{"url_contains": ...}``) makes it a LOCATE loop that
        stops at the first page matching it."""
        _auth(authorization)
        if sid not in app.state.sessions:
            return _error(404, "NoSuchSession", f"no session {sid!r}", hint=_SESSION_HINT)
        import uuid

        seeds_in = body.get("seeds") or body.get("seed") or []
        seeds = [seeds_in] if isinstance(seeds_in, str) else list(seeds_in)
        if not seeds:
            return _error(422, "InvalidRequest", "provide 'seeds' (a URL or a list of URLs)")
        mode = body.get("mode", "auto")
        kw: dict[str, Any] = {k: body[k] for k in ("max_pages", "width", "depth", "keywords", "obey_robots", "browser", "scope", "same_origin") if k in body}
        kw["auto"] = mode != "manual"
        session = app.state.sessions[sid]
        try:
            crawl = session.crawl(seeds, **kw)
        except (TypeError, ValueError) as exc:
            return _error(422, "InvalidRequest", str(exc), hint="check the crawl options")
        crawl.__enter__()
        cid = uuid.uuid4().hex[:12]
        goal = body.get("goal")
        app.state.crawls[cid] = {"crawl": crawl, "session": sid, "mode": mode, "seeds": seeds, "goal": goal}
        if goal:
            from .core.crawl import locate as _locate

            def until(card: Any) -> bool:
                title = str(getattr(card, "title", "") or "").lower()
                url = str(getattr(card, "final_url", None) or getattr(card, "url", "") or "").lower()
                ok = True
                if goal.get("title_contains"):
                    ok = ok and str(goal["title_contains"]).lower() in title
                if goal.get("url_contains"):
                    ok = ok and str(goal["url_contains"]).lower() in url
                return ok
            _run_in_thread(cid, lambda: _locate(crawl, until, stop_on_first=not goal.get("all", False)))
        elif mode != "manual":
            _run_in_thread(cid, crawl.run)
        log.info("crawl %s started in session %s (%s, %d seed(s))", cid, sid, mode, len(seeds))
        return _crawl_state(cid)

    @app.get("/sessions/{sid}/crawls", response_model=None)
    def list_crawls(sid: str, authorization: str | None = Header(default=None)) -> "list[dict[str, Any]] | JSONResponse":
        _auth(authorization)
        return [_crawl_state(cid) for cid, h in app.state.crawls.items() if h["session"] == sid]

    @app.get("/crawls/{cid}", response_model=None)
    def get_crawl(cid: str, authorization: str | None = Header(default=None)) -> "dict[str, Any] | JSONResponse":
        """The crawl's state: pages, the scored frontier (with the page each edge came from),
        failures, the round, a pending Ask, the locate result."""
        _auth(authorization)
        if cid not in app.state.crawls:
            return _error(404, "InvalidRequest", f"no crawl {cid!r}")
        return _crawl_state(cid)

    @app.post("/crawls/{cid}/step", response_model=None)
    def step_crawl(cid: str, body: dict[str, Any], authorization: str | None = Header(default=None)) -> "dict[str, Any] | JSONResponse":
        """Fetch one round: ``picks`` (frontier URLs, or new ones) -- none = the best-first
        top ``width`` in auto mode, nothing in manual mode."""
        _auth(authorization)
        if cid not in app.state.crawls:
            return _error(404, "InvalidRequest", f"no crawl {cid!r}")
        held = app.state.crawls[cid]
        if held.get("thread") and held["thread"].is_alive():
            return _error(409, "InvalidRequest", "the crawl is running; wait or stop it", retriable=True)
        picks = body.get("picks") or None
        try:
            held["crawl"].step(picks)
        except WebException as exc:
            return _error(502, exc.error.type, str(exc), error=exc.error)
        return _crawl_state(cid)

    @app.post("/crawls/{cid}/run", response_model=None)
    def run_crawl(cid: str, authorization: str | None = Header(default=None)) -> "dict[str, Any] | JSONResponse":
        """Drive the crawl to its budget in the background."""
        _auth(authorization)
        if cid not in app.state.crawls:
            return _error(404, "InvalidRequest", f"no crawl {cid!r}")
        held = app.state.crawls[cid]
        if not (held.get("thread") and held["thread"].is_alive()):
            _run_in_thread(cid, held["crawl"].run)
        return _crawl_state(cid)

    @app.post("/crawls/{cid}/resume", response_model=None)
    def resume_crawl(cid: str, body: dict[str, Any], authorization: str | None = Header(default=None)) -> "dict[str, Any] | JSONResponse":
        """Answer a waiting Ask with ``picks`` and continue."""
        _auth(authorization)
        if cid not in app.state.crawls:
            return _error(404, "InvalidRequest", f"no crawl {cid!r}")
        held = app.state.crawls[cid]
        picks = body.get("picks") or body.get("answer")
        _run_in_thread(cid, lambda: held["crawl"].resume(picks if isinstance(picks, list) else [picks] if picks else None))
        return _crawl_state(cid)

    @app.delete("/crawls/{cid}", response_model=None)
    def close_crawl(cid: str, authorization: str | None = Header(default=None)) -> "dict[str, Any] | JSONResponse":
        _auth(authorization)
        held = app.state.crawls.pop(cid, None)
        if held is None:
            return _error(404, "InvalidRequest", f"no crawl {cid!r}")
        try:
            held["crawl"].close()
        except Exception:  # noqa: BLE001 - already closed
            pass
        return {"id": cid, "status": "closed"}

    @app.get("/loops", response_model=None)
    def loops(authorization: str | None = Header(default=None)) -> "list[dict[str, Any]] | JSONResponse":
        """The loops on this engine waiting for a human decision (an ``Ask``): a crawl
        waiting for picks, a document mid-ladder waiting for a tier."""
        _auth(authorization)
        out: list[dict[str, Any]] = []
        for key, obj in app.state.wc._the_engine().waiting.items():
            ask = getattr(obj, "pending", None)
            if ask is None:
                continue
            kind = "crawl" if hasattr(obj, "frontier") else "resolve"
            out.append({"id": key, "kind": kind, "ask": ask.model_dump(mode="json")})
        return out

    @app.post("/loops/{loop_id}/resume", response_model=None)
    def resume_loop(loop_id: str, body: dict[str, Any], authorization: str | None = Header(default=None)) -> "dict[str, Any] | JSONResponse":
        """Answer a waiting loop: ``{"answer": ...}`` -- for a crawl the picks (a URL or a list),
        for a document mid-ladder the tier (``"browser"`` / ``"proxy"``)."""
        _auth(authorization)
        engine = app.state.wc._the_engine()
        obj = engine.waiting.get(loop_id)
        if obj is None or getattr(obj, "pending", None) is None:
            return _error(404, "InvalidRequest", f"no waiting loop {loop_id!r}", hint="GET /loops lists them")
        answer = body.get("answer")
        try:
            if hasattr(obj, "frontier"):  # a crawl
                picks = answer if isinstance(answer, list) else ([answer] if answer else [])
                obj.resume(picks, run=bool(body.get("run", True)))
                return {"id": loop_id, "kind": "crawl", "pages": len(obj.pages), "waiting": obj.pending is not None}
            doc = app.state.wc.escalate(obj, str(answer))
            return {"id": loop_id, "kind": "resolve", "ok": doc.ok, "tiers": list(doc._tiers)}
        except WebException as exc:
            return _error(502, exc.error.type, str(exc), retriable=exc.error.retriable, error=exc.error)

    def _wire(event: Any) -> dict[str, Any]:
        """An event as JSON for the socket -- the trace's own wire view (payloads dropped)."""
        from .trace import wire

        return wire(event)

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

            stored = Path(app.state.traces_dir) / f"{trace_path}.jsonl"  # an id from /traces, or a path
            if "/" not in trace_path and stored.is_file():
                trace_path = str(stored)
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
