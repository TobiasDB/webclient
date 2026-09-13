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
from typing import Any

from fastapi import FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect

from .expr import from_plan
from .surfaces import Document, Reference, WebClient


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


def create_app(wc: WebClient | None = None, token: str | None = None) -> FastAPI:
    """A FastAPI app exposing a WebClient over ``/execute`` (Bearer-token
    authorised when ``token`` is set). An existing client may be supplied."""
    app = FastAPI()
    app.state.wc = wc if wc is not None else WebClient()
    app.state.docs = {}
    app.state.sessions = {}

    def _auth(authorization: str | None) -> None:
        if token is not None and authorization != f"Bearer {token}":
            raise HTTPException(status_code=401, detail="bad token")

    @app.post("/execute")
    def execute(
        body: dict[str, Any], authorization: str | None = Header(default=None)
    ) -> dict[str, Any]:
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
        elif sid and sid in app.state.sessions:  # resolve through the session
            context = app.state.sessions[sid]._core
        elif "url" in body:
            context = wc_.ref(body["url"])
        else:
            context = None
        result = wc_.execute(expr, context)
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
        queue: asyncio.Queue = asyncio.Queue()

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
