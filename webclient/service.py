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
import time
from collections import OrderedDict
from typing import Any

from fastapi import FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

from .errors import WebException
from .expr import from_plan
from .surfaces import Document, Reference, WebClient


class _DocStore(OrderedDict[str, Any]):
    """An LRU-capped id->Document store, so a long-lived server does not retain
    every document it ever returned. Evicting a stale id just means a follow-up
    plan rooted at it gets a 404 (the client re-fetches)."""

    def __init__(self, cap: int) -> None:
        super().__init__()
        self._cap = cap

    def __setitem__(self, key: str, value: Any) -> None:
        super().__setitem__(key, value)
        self.move_to_end(key)
        while len(self) > self._cap:
            self.popitem(last=False)  # evict least-recently-used

    def __getitem__(self, key: str) -> Any:
        value = super().__getitem__(key)
        self.move_to_end(key)  # LRU touch on access
        return value


def _serialize(value: Any, store: dict[str, Any]) -> Any:
    """A Document -> a stored handle; a Reference -> its url; a Field -> its
    value; a Collection/list/dict recurse; scalars pass through."""
    from .collection import Collection, Field

    if isinstance(value, Field):
        return value.get()
    if isinstance(value, Collection):
        return [_serialize(v, store) for v in value]
    if isinstance(value, Document):
        store[value.name] = value
        handle = {"id": value.name, "kind": value.kind, "ok": value.ok}
        if value.kind in ("html", "xml"):
            handle["title"] = value.title
        return {"__doc__": handle}
    if isinstance(value, Reference):
        return value.url
    if isinstance(value, list):
        return [_serialize(v, store) for v in value]
    if isinstance(value, dict):
        return {k: _serialize(v, store) for k, v in value.items()}
    return value


def create_app(
    wc: WebClient | None = None,
    token: str | None = None,
    max_docs: int = 1024,
    max_sessions: int = 256,
    block_private_hosts: bool = False,
) -> FastAPI:
    """A FastAPI app exposing a WebClient over ``/execute`` (Bearer-token
    authorised when ``token`` is set). An existing client may be supplied;
    ``max_docs`` caps the LRU document store, ``max_sessions`` bounds the
    live-session store (expired/closed sessions are reclaimed first), and
    ``block_private_hosts`` turns on the SSRF guard for a hosted server (refuses
    plans that resolve to loopback/private hosts)."""
    app = FastAPI()
    app.state.wc = (
        wc if wc is not None else WebClient(block_private_hosts=block_private_hosts)
    )
    app.state.docs = _DocStore(max_docs)
    app.state.sessions = {}

    def _auth(authorization: str | None) -> None:
        if token is not None and authorization != f"Bearer {token}":
            raise HTTPException(status_code=401, detail="bad token")

    def _sweep_sessions() -> None:
        """Drop closed or past-ttl sessions so the store does not leak them."""
        now = time.time()
        for sid, s in list(app.state.sessions.items()):
            expired = s.expires_at is not None and now > s.expires_at
            if s.status != "running" or expired:
                app.state.sessions.pop(sid, None)

    @app.post("/execute", response_model=None)
    def execute(
        body: dict[str, Any], authorization: str | None = Header(default=None)
    ) -> "dict[str, Any] | JSONResponse":
        _auth(authorization)
        wc_: WebClient = app.state.wc
        try:
            expr = from_plan(body["plan"], wc_._core)
        except ValueError as exc:  # unknown root / private name
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        sid = expr._plan.session_id
        if "document_id" in body:
            if body["document_id"] not in app.state.docs:
                raise HTTPException(status_code=404, detail="no such document")
            context: Any = app.state.docs[body["document_id"]]
        elif "context_plan" in body:  # a client ref/fetch context plan
            context = from_plan(body["context_plan"], wc_._core)
        elif sid and sid in app.state.sessions:  # resolve through the session
            context = app.state.sessions[sid]._core
        elif "url" in body:
            context = wc_.ref(body["url"])
        else:
            context = None
        try:
            result = wc_._core.execute(expr, context)  # the realization machinery
        except WebException as exc:  # a fetch/resolve failure -> structured error
            err = exc.error
            return JSONResponse(
                status_code=502,
                content={
                    "error": {
                        "type": err.type,
                        "message": str(exc),
                        "status_code": err.status_code,
                        "retriable": err.retriable,
                    }
                },
            )
        return {"rows": _serialize(result, app.state.docs)}

    @app.get("/document/{doc_id}")
    def document(
        doc_id: str, authorization: str | None = Header(default=None)
    ) -> dict[str, Any]:
        _auth(authorization)
        if doc_id not in app.state.docs:
            raise HTTPException(status_code=404, detail="no such document")
        d = app.state.docs[doc_id]
        return {"id": d.name, "kind": d.kind, "ok": d.ok}

    # -- sessions ------------------------------------------------------------
    @app.post("/sessions")
    def create_session(
        body: dict[str, Any], authorization: str | None = Header(default=None)
    ) -> dict[str, Any]:
        _auth(authorization)
        _sweep_sessions()  # reclaim expired/closed before enforcing the cap
        if len(app.state.sessions) >= max_sessions:
            raise HTTPException(status_code=429, detail="too many sessions")
        session = app.state.wc.session(ttl=body.get("ttl"))
        app.state.sessions[session.id] = session
        return {"id": session.id, "status": session.status}

    @app.get("/sessions/{sid}")
    def get_session(
        sid: str, authorization: str | None = Header(default=None)
    ) -> dict[str, Any]:
        _auth(authorization)
        if sid not in app.state.sessions:
            raise HTTPException(status_code=404, detail="no such session")
        return {"id": sid, "status": app.state.sessions[sid].status}

    @app.delete("/sessions/{sid}")
    def close_session(
        sid: str, authorization: str | None = Header(default=None)
    ) -> dict[str, Any]:
        _auth(authorization)
        if sid not in app.state.sessions:
            raise HTTPException(status_code=404, detail="no such session")
        app.state.sessions[sid].close()
        return {"id": sid, "status": "closed"}

    @app.post("/crawl")
    def crawl(authorization: str | None = Header(default=None)) -> dict[str, Any]:
        _auth(authorization)
        raise HTTPException(status_code=501, detail="crawl is not implemented")

    # -- live event stream ---------------------------------------------------
    @app.websocket("/events")
    async def events(ws: WebSocket) -> None:
        await ws.accept()
        topic = ws.query_params.get("topic", "")
        loop = asyncio.get_event_loop()
        queue: asyncio.Queue[Any] = asyncio.Queue()

        def handler(event: Any) -> None:  # engine thread -> server loop
            loop.call_soon_threadsafe(queue.put_nowait, event.model_dump(mode="json"))

        sub = app.state.wc.bus.subscribe(topic, handler)
        try:
            while True:
                await ws.send_json(await queue.get())
        except WebSocketDisconnect:
            pass
        finally:
            sub.cancel()

    return app


__all__ = ["create_app"]
