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

from .errors import WebException
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
) -> JSONResponse:
    """A structured, agent-actionable error body: an autonomous caller branches on
    ``type``/``retriable`` and follows ``hint`` instead of parsing a string. The
    inner ``status_code`` defaults to the HTTP status but carries the *upstream*
    status for a proxied fetch failure (a 502 wrapping an origin 500)."""
    body: dict[str, Any] = {
        "type": type_,
        "message": message,
        "status_code": http_status if status_code is None else status_code,
        "retriable": retriable,
    }
    if hint is not None:
        body["hint"] = hint
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

    # -- crawl / sitemap -----------------------------------------------------
    def _resolve_of(value: Any) -> "Any":
        """Rebuild a :class:`Resolve` policy bundle from a request body's ``resolve``
        (a dict from the remote client's ``model_dump``); ``None`` when absent."""
        if not value:
            return None
        from .policy import Resolve

        return Resolve.model_validate(value)

    def _crawl_engine(body: dict[str, Any]) -> "Any":
        """The engine a crawl runs on: a named ``session`` (so it fetches with that
        session's identity / cookies -- e.g. crawling behind a login) or, by
        default, the shared client. Returns the engine, or a JSONResponse error if
        a ``session`` was named but is unknown."""
        sid = body.get("session")
        if sid is None:
            return app.state.wc
        if sid not in app.state.sessions:
            return _error(404, "NoSuchSession", f"no session {sid!r}", hint=_SESSION_HINT)
        return app.state.sessions[sid]

    def _run_crawl(engine: "Any", body: dict[str, Any]) -> "Any":
        """Build and run a crawl on ``engine`` (a client or session); the caller
        turns the finished crawl into a response. Raises WebException on a failure."""
        url = body["url"]
        resolve = _resolve_of(body.get("resolve"))
        return engine.crawl(
            url,
            auto=True,  # the HTTP tier runs a bounded auto crawl (Firecrawl-shaped)
            width=int(body.get("width", 10)),
            depth=int(body.get("depth", 3)),
            max_pages=int(body.get("max_pages", 20)),
            max_frontier=int(body.get("max_frontier", 10000)),
            same_origin=bool(body.get("same_origin", True)),
            obey_robots=bool(body.get("obey_robots", True)),
            browser=bool(body.get("browser", False)),
            resolve=resolve,
            keywords=body.get("keywords"),
            include=body.get("include"),
            exclude=body.get("exclude"),
        ).run()

    def _crawl_response(crawl: Any) -> "dict[str, Any]":
        """The crawl result over the wire: a lean per-page record (the crawl's default
        :class:`PageCard` projection -- url / status / kind / title) plus the unresolved
        frontier edges, capped to ``width`` (best-first) so a large crawl doesn't flood
        the client (``frontier_total`` is the true count)."""
        def page(card: Any) -> dict[str, Any]:
            return {
                "url": card.final_url or card.url,
                "status": card.status_code,
                "kind": card.kind,
                "title": card.title,
            }

        return {
            "pages": [page(p) for p in crawl.pages],
            "urls": [p.final_url or p.url for p in crawl.pages],
            "frontier": [e.model_dump() for e in crawl.frontier[: crawl.config.width]],
            "frontier_total": len(crawl.frontier),
            "done": crawl.done,
        }

    @app.post("/crawl", response_model=None)
    def crawl(
        body: dict[str, Any], authorization: str | None = Header(default=None)
    ) -> "dict[str, Any] | JSONResponse":
        """Bounded, same-origin crawl from ``url`` -> a lean handle per page plus
        the unresolved frontier. Steer it with ``keywords`` (best-first),
        ``include``/``exclude``, ``max_pages``/``depth``/``width``."""
        _auth(authorization)
        if not body.get("url"):
            return _error(
                422,
                "InvalidRequest",
                "crawl requires a 'url'",
                hint='POST {"url": "https://...", "max_pages": 20, '
                '"keywords": ["pricing"], "session": "sess-..."}',
            )
        engine = _crawl_engine(body)
        if isinstance(engine, JSONResponse):
            return engine
        try:
            return _crawl_response(_run_crawl(engine, body))
        except WebException as exc:
            return _error(
                502,
                exc.error.type,
                str(exc),
                retriable=exc.error.retriable,
                status_code=exc.error.status_code,
                hint="retry if retriable; else the seed is unavailable or blocked",
            )

    # A remote crawl is not a stateful server-side object any more: the client runs the
    # whole crawl as ONE ``WebClient.crawl(seeds, ...).run().pages`` plan over /execute
    # (see ``Crawl._remote_call``), so there is no /crawls create/step/run/delete surface.
    # The one-shot /crawl task verb below stays (a ready-to-use value for an agent).

    # -- task verbs: ready-to-use values for an agent (no plan machinery) -----
    def _verb_url(body: dict[str, Any]) -> "str | JSONResponse":
        if not body.get("url"):
            return _error(
                422, "InvalidRequest", "this verb requires a 'url'",
                hint='POST {"url": "https://..."}',
            )
        return str(body["url"])

    def _run_verb(fn: Any) -> "dict[str, Any] | JSONResponse":
        """Run a task verb, mapping a fetch/resolve failure to a structured error."""
        try:
            return {"result": fn()}
        except WebException as exc:
            return _error(
                502, exc.error.type, str(exc),
                retriable=exc.error.retriable, status_code=exc.error.status_code,
                hint="retry if retriable; else the target is unavailable or blocked",
            )

    @app.post("/markdown", response_model=None)
    def markdown(
        body: dict[str, Any], authorization: str | None = Header(default=None)
    ) -> "dict[str, Any] | JSONResponse":
        """Fetch ``url`` and return its content as markdown."""
        _auth(authorization)
        url = _verb_url(body)
        if isinstance(url, JSONResponse):
            return url
        wc_: WebClient = app.state.wc
        return _run_verb(lambda: wc_.fetch(url).render("markdown"))

    @app.post("/text", response_model=None)
    def text(
        body: dict[str, Any], authorization: str | None = Header(default=None)
    ) -> "dict[str, Any] | JSONResponse":
        """Fetch ``url`` and return its readable text (chrome stripped)."""
        _auth(authorization)
        url = _verb_url(body)
        if isinstance(url, JSONResponse):
            return url
        wc_: WebClient = app.state.wc
        return _run_verb(
            lambda: wc_.fetch(url).render(
                "text", main_content_only=body.get("main_content_only", True)
            )
        )

    @app.post("/links", response_model=None)
    def links(
        body: dict[str, Any], authorization: str | None = Header(default=None)
    ) -> "dict[str, Any] | JSONResponse":
        """Fetch ``url`` and return its outbound link URLs (absolute)."""
        _auth(authorization)
        url = _verb_url(body)
        if isinstance(url, JSONResponse):
            return url
        wc_: WebClient = app.state.wc
        return _run_verb(lambda: [r.url for r in wc_.fetch(url).render("links")])

    @app.post("/skeleton", response_model=None)
    def skeleton(
        body: dict[str, Any], authorization: str | None = Header(default=None)
    ) -> "dict[str, Any] | JSONResponse":
        """Fetch ``url`` and return its token-lean DOM skeleton (for writing CSS
        selectors). ``browser`` (e.g. ``"probe"``) renders a JS/SPA page and marks
        client-injected nodes."""
        _auth(authorization)
        url = _verb_url(body)
        if isinstance(url, JSONResponse):
            return url
        wc_: WebClient = app.state.wc
        browser = body.get("browser", False)
        return _run_verb(lambda: wc_.fetch(url, browser=browser).skeleton())

    @app.post("/sitemap", response_model=None)
    def sitemap(
        body: dict[str, Any], authorization: str | None = Header(default=None)
    ) -> "dict[str, Any] | JSONResponse":
        """Hunt a site's sitemap.xml page URLs from ``url`` (cheap -- not a crawl)."""
        _auth(authorization)
        url = _verb_url(body)
        if isinstance(url, JSONResponse):
            return url
        wc_: WebClient = app.state.wc
        return _run_verb(lambda: [r.url for r in wc_.sitemap(url)])

    @app.post("/robots", response_model=None)
    def robots(
        body: dict[str, Any], authorization: str | None = Header(default=None)
    ) -> "dict[str, Any] | JSONResponse":
        """Hunt a site's robots.txt from ``url`` -- its Sitemap: URLs and raw rules."""
        _auth(authorization)
        url = _verb_url(body)
        if isinstance(url, JSONResponse):
            return url
        wc_: WebClient = app.state.wc
        return _run_verb(lambda: wc_.robots(url).model_dump())

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
                )
            out["rows"] = _serialize(result, app.state.docs)
        return out

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
