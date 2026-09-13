"""The WebClient behind an HTTP API -- browser as a service.

Every operation is one Plan POSTed to ``/execute`` with either a ``url`` (root
a fetch) or a ``document_id`` (continue from a server-side document). A returned
Document crosses the wire as a handle ``{"__doc__": {...}}`` whose content is
reached only by a further plan rooted at that handle's id; scalars/rows return
inline. The plan is the same wire form ``Plan.model_dump()`` the client records.
"""
from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Header, HTTPException

from .expr import from_plan
from .surfaces import Document, Reference, WebClient


def _serialize(value: Any, store: dict[str, Any]) -> Any:
    """A Document -> a stored handle; a Reference -> its url; lists/dicts
    recurse; scalars pass through."""
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


def create_app(token: str | None = None) -> FastAPI:
    """A FastAPI app exposing the WebClient over ``/execute`` (Bearer-token
    authorised when ``token`` is set)."""
    app = FastAPI()
    app.state.wc = WebClient()
    app.state.docs = {}

    @app.post("/execute")
    def execute(body: dict[str, Any],
                authorization: str | None = Header(default=None)) -> dict[str, Any]:
        if token is not None and authorization != f"Bearer {token}":
            raise HTTPException(status_code=401, detail="bad token")
        wc: WebClient = app.state.wc
        if "document_id" in body:
            context: Any = app.state.docs[body["document_id"]]
        elif "url" in body:
            context = wc.ref(body["url"])
        else:
            context = None
        expr = from_plan(body["plan"], wc._core)
        result = wc.execute(expr, context)
        return {"rows": _serialize(result, app.state.docs)}

    return app


__all__ = ["create_app"]
