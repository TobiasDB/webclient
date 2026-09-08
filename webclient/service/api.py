"""HTTP/WS service: browser as a service (M7).

A thin FastAPI adapter over a WebClient -- no logic beyond (de)serialization
and auth. Documents are handles: /fetch returns an id + metadata, and
content crosses the wire only via /render (markdown/text/elements/links/html)
or /select, never as raw HTML by default. Plans are submitted as QueryPlan
JSON. Events stream over a websocket, resumable via ?after=seq so a consumer
can detect gaps and rebuild (dom.snapshot checkpoints carry a digest).

    from webclient.service import create_app
    app = create_app(token="secret")        # a WebClient per app

The raw-CDP passthrough (WS /sessions/{id}/cdp) is specified but deferred
(ISSUES #33): it needs the browser's CDP endpoint proxied, out of scope for
this pass.
"""
from __future__ import annotations

import asyncio
from collections import OrderedDict
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, WebSocket
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ..client import WebClient
from ..lazy.expr import QueryPlan
from ..models import Reference


class FetchBody(BaseModel):
    url: str
    method: str = "get"
    browser: bool = False
    session_id: str | None = None
    headers: dict[str, str] = {}
    optional: bool = False


class SessionBody(BaseModel):
    ttl: float | None = None
    keep_alive: bool = False
    headers: dict[str, str] = {}


class SelectBody(BaseModel):
    selector: str
    all: bool = False
    attr: str | None = None          # None -> element text


def _doc_meta(doc: Any) -> dict[str, Any]:
    return {
        "id": doc.id,
        "session_id": doc.session_id,
        "kind": doc.kind,
        "status_code": doc.status_code,
        "ok": doc.ok,
        "final_url": doc.final_url,
        "title": doc.html.title if doc.kind == "html" else None,
    }


def create_app(client: WebClient | None = None, *,
               token: str | None = None) -> FastAPI:
    app = FastAPI(title="webclient service")
    wc = client or WebClient()
    app.state.wc = wc
    # The service returns only ids, so unlike the library it must hold its
    # documents (the library keeps them by weakref, assuming the caller has
    # a strong ref). Bounded LRU cache.
    docs: "OrderedDict[str, Any]" = OrderedDict()
    docs_cap = 256

    def remember(doc: Any) -> Any:
        docs[doc.id] = doc
        docs.move_to_end(doc.id)
        while len(docs) > docs_cap:
            docs.popitem(last=False)
        return doc

    def lookup(document_id: str) -> Any:
        doc = docs.get(document_id) or wc.document(document_id)
        if doc is None:
            raise HTTPException(status_code=404, detail="no such document")
        docs.move_to_end(document_id, last=True) if document_id in docs else None
        return doc

    def auth(authorization: str = Header(default="")) -> None:
        if token is None:
            return
        if authorization != f"Bearer {token}":
            raise HTTPException(status_code=401, detail="bad token")

    def _session(session_id: str | None) -> Any:
        if session_id is None:
            return None
        sess = wc._sessions.get(session_id)
        if sess is None:
            raise HTTPException(status_code=404, detail="no such session")
        return sess

    # -- sessions ------------------------------------------------------------
    @app.post("/sessions", dependencies=[Depends(auth)])
    def create_session(body: SessionBody) -> dict[str, Any]:
        sess = wc.session(ttl=body.ttl, keep_alive=body.keep_alive,
                          headers=body.headers)
        return {"id": sess.id, "status": sess.status,
                "expires_at": sess.expires_at}

    @app.get("/sessions/{session_id}", dependencies=[Depends(auth)])
    def get_session(session_id: str) -> dict[str, Any]:
        sess = _session(session_id)
        return {"id": sess.id, "status": sess.status,
                "expires_at": sess.expires_at}

    @app.delete("/sessions/{session_id}", dependencies=[Depends(auth)])
    def close_session(session_id: str) -> dict[str, Any]:
        sess = _session(session_id)
        sess.close()
        return {"id": sess.id, "status": sess.status}

    # -- fetch / documents ---------------------------------------------------
    @app.post("/fetch", dependencies=[Depends(auth)])
    def fetch(body: FetchBody) -> dict[str, Any]:
        ref = Reference.from_url(body.url, method=body.method,  # type: ignore[arg-type]
                                 headers=body.headers).bind(wc)
        doc = wc.fetch(ref, browser=body.browser,
                       session=_session(body.session_id),
                       optional=body.optional)
        return _doc_meta(remember(doc))

    @app.get("/documents/{document_id}", dependencies=[Depends(auth)])
    def get_document(document_id: str) -> dict[str, Any]:
        return _doc_meta(lookup(document_id))

    @app.get("/documents/{document_id}/render", dependencies=[Depends(auth)])
    def render(document_id: str, format: str = "markdown",
               main_content_only: bool = False) -> JSONResponse:
        doc = lookup(document_id)
        result = doc.render(format, main_content_only=main_content_only)
        if hasattr(result, "__iter__") and not isinstance(result, str):
            result = [r.url if isinstance(r, Reference)
                      else (r.model_dump() if isinstance(r, BaseModel) else r)
                      for r in result]
        return JSONResponse({"format": format, "result": result})

    @app.post("/documents/{document_id}/select", dependencies=[Depends(auth)])
    def select(document_id: str, body: SelectBody) -> dict[str, Any]:
        doc = lookup(document_id)

        def value(node: Any) -> Any:
            if body.attr is None:
                return node.text
            got = node.attr(body.attr, optional=True)
            return got.url if isinstance(got, Reference) else got

        if body.all:
            return {"values": [value(n) for n in doc.select_all(body.selector)]}
        node = doc.select(body.selector, optional=True)
        return {"value": value(node) if node is not None else None}

    # -- plans ---------------------------------------------------------------
    @app.post("/plans", dependencies=[Depends(auth)])
    def run_plan(plan: QueryPlan, url: str,
                 session_id: str | None = None) -> dict[str, Any]:
        from ..lazy.expr import Expr
        expr = Expr.from_query(plan)
        ref = Reference.from_url(url).bind(wc, _session(session_id))
        rows = wc.execute(expr, ref)
        return {"rows": _jsonable(rows)}

    # -- events (resumable stream) -------------------------------------------
    @app.websocket("/events")
    async def events(ws: WebSocket, topic: str = "", document_id: str | None = None,
                     after: int = 0) -> None:
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
                    "node_id": event.node_id,
                })
        except Exception:
            pass
        finally:
            sub.cancel()

    return app


def _jsonable(value: Any) -> Any:
    if isinstance(value, Reference):
        return value.url
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_jsonable(v) for v in value]
    return value
