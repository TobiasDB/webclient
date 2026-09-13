"""HTTP/WS service: the WebClient/WebSession interface over HTTP (PLAN §5d).

Thin FastAPI adapter over a WebClient. Every operation is one Plan submitted
to ``/execute`` and run inside a WebSession -- fetch is ``ref.resolve()``,
search is a projected plan, and so on, exactly as the local facade builds
them. A Document produced by a plan crosses the wire as a handle
(``{"__doc__": meta}``) the remote client rehydrates; its content only
crosses via a further Plan rooted at that handle's id, never as raw HTML.
The Plan is validated against the op registry before it runs. Events stream
over a resumable websocket.

    from webclient.service import create_app
    app = create_app(token="secret")
"""
from __future__ import annotations

import asyncio
from collections import OrderedDict
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, WebSocket
from pydantic import BaseModel

from ..core.document import Collection, Document, Field, Reference, WebBase
from ..core.expr import Plan, from_plan
from ..core.engine import WebClient


class SessionBody(BaseModel):
    ttl: float | None = None
    keep_alive: bool = False
    headers: dict[str, str] = {}


class ExecuteBody(BaseModel):
    plan: Plan
    url: str | None = None           # root a context plan at this URL
    document_id: str | None = None   # ...or at a server-side document
    session_id: str | None = None


def _doc_meta(doc: Any) -> dict[str, Any]:
    return {"id": doc.id, "session_id": doc.session_id, "kind": doc.kind,
            "status_code": doc.status_code, "ok": doc.ok,
            "final_url": doc.final_url,
            "title": doc.title if doc.kind == "html" else None}


def create_app(client: WebClient | None = None, *,
               token: str | None = None) -> FastAPI:
    app = FastAPI(title="webclient service")
    wc = client or WebClient()
    app.state.wc = wc
    docs: "OrderedDict[str, Any]" = OrderedDict()   # server holds its documents
    docs_cap = 256
    default = wc.session()                          # per-app default session

    def remember(doc: Any) -> Any:
        docs[doc.id] = doc
        docs.move_to_end(doc.id)
        while len(docs) > docs_cap:
            docs.popitem(last=False)
        return doc

    def lookup(document_id: str) -> Any:
        doc = docs.get(document_id) or wc.document(document_id)
        if doc is None:
            raise HTTPException(404, "no such document")
        return doc

    def serialize(value: Any) -> Any:
        """Result of an executed plan, made jsonable: a Document becomes a
        remembered handle (``{"__doc__": meta}``) the remote client rehydrates;
        everything else projects to plain data."""
        if isinstance(value, Document):
            remember(value)
            return {"__doc__": _doc_meta(value)}
        if isinstance(value, Reference):
            return value.url
        if isinstance(value, Field):
            return value.value if value.ok else None
        if isinstance(value, Collection):
            return [serialize(v) for v in value]
        if isinstance(value, WebBase):
            return serialize(value.project())
        if isinstance(value, dict):
            return {k: serialize(v) for k, v in value.items()}
        if isinstance(value, list):
            return [serialize(v) for v in value]
        return value

    def auth(authorization: str = Header(default="")) -> None:
        if token is not None and authorization != f"Bearer {token}":
            raise HTTPException(401, "bad token")

    def use_session(session_id: str | None) -> Any:
        if session_id is None:
            return default
        sess = wc._sessions.get(session_id)
        if sess is None:
            raise HTTPException(404, "no such session")
        return sess

    # -- sessions ------------------------------------------------------------
    @app.post("/sessions", dependencies=[Depends(auth)])
    def create_session(body: SessionBody) -> dict[str, Any]:
        sess = wc.session(ttl=body.ttl, keep_alive=body.keep_alive,
                          headers=body.headers)
        return {"id": sess.id, "status": sess.status, "expires_at": sess.expires_at}

    @app.get("/sessions/{session_id}", dependencies=[Depends(auth)])
    def get_session(session_id: str) -> dict[str, Any]:
        sess = use_session(session_id)
        return {"id": sess.id, "status": sess.status, "expires_at": sess.expires_at}

    @app.delete("/sessions/{session_id}", dependencies=[Depends(auth)])
    def close_session(session_id: str) -> dict[str, Any]:
        sess = use_session(session_id)
        sess.close()
        return {"id": sess.id, "status": sess.status}

    # -- document handles ----------------------------------------------------
    @app.get("/document/{document_id}", dependencies=[Depends(auth)])
    def get_document(document_id: str) -> dict[str, Any]:
        return _doc_meta(lookup(document_id))

    # -- execute a plan (rooted at a URL or a server-side document) -----------
    @app.post("/execute", dependencies=[Depends(auth)])
    def execute(body: ExecuteBody) -> dict[str, Any]:
        sess = use_session(body.session_id)
        try:
            expr = from_plan(body.plan, client=wc.core)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        # a document-source plan self-carries the handle id in plan.source
        doc_id = body.document_id or (body.plan.source or {}).get("document_id")
        if doc_id is not None:
            context: Any = lookup(doc_id)
        elif body.url is not None:
            context = sess.ref(body.url)
        else:
            context = None
        return {"rows": serialize(sess.execute(expr, context))}

    # -- crawl ---------------------------------------------------------------
    @app.post("/crawl", dependencies=[Depends(auth)])
    def crawl() -> dict[str, Any]:
        raise HTTPException(501, "crawl is not implemented")

    # -- events (resumable stream) -------------------------------------------
    @app.websocket("/events")
    async def events(ws: WebSocket, topic: str = "",
                     document_id: str | None = None, after: int = 0) -> None:
        await ws.accept()
        queue: asyncio.Queue = asyncio.Queue()
        loop = asyncio.get_event_loop()

        def on_event(event: Any) -> None:
            if event.seq is not None and event.seq <= after:
                return
            loop.call_soon_threadsafe(queue.put_nowait, event)

        sub = wc.bus.subscribe(topic, on_event, document_id=document_id)
        try:
            while True:
                event = await queue.get()
                await ws.send_json({
                    "topic": event.topic, "seq": event.seq,
                    "source": event.source, "document_id": event.document_id,
                    "payload": event.model_dump(exclude={
                        "topic", "seq", "source", "document_id", "session_id",
                        "plan_id", "ts", "node_id"}),
                    "node_id": event.node_id})
        except Exception:
            pass
        finally:
            sub.cancel()

    return app
