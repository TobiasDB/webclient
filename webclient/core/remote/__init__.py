"""Remote backend: a ``WebClientCore`` that executes over HTTP.

Remote is not a separate client -- it is the same surface over a swapped core.
``RemoteWebClientCore`` is a ``WebClientCore`` whose ``execute`` POSTs the plan
to a ``webclient.service`` app instead of running it on a local engine, so a
``WebClient`` over it builds the very same plans (the inherited fetch backing)
with no local browser or lxml -- only httpx + pydantic. A fetched document comes
back as a shallow handle (``_RemoteDoc``): metadata (title/ok/kind) inline, any
op a plan rooted at the server-side document id, run with one more round trip.
"""

from __future__ import annotations

from typing import Any, cast

import httpx
from pydantic import PrivateAttr

from ...query.expr import Expr
from ...query.plan import Plan
from ..client import WebClientCore, _materialize
from ..reference import HttpMethod, ReferenceCore
from ..reference import from_url as _core_from_url


def _url_of(source: dict[str, Any]) -> str:
    return cast(str, ReferenceCore(**source).dispatch("url"))


class _RemoteDoc:
    """A server-side document handle -- the eager remote surface. Metadata
    (title/ok/kind/id) is inline; every other attribute/op round-trips a plan
    rooted at the handle's id and returns the materialised value (a value attr ->
    its value, a call op -> a value / handle / a ``Collection`` of handles), so it
    matches the eager ``Document`` type. Chain a batch through ``.lazy`` (one plan,
    one round-trip) rather than a round-trip per op."""

    def __init__(self, meta: dict[str, Any], core: "RemoteWebClientCore") -> None:
        object.__setattr__(self, "_meta", meta)
        object.__setattr__(self, "_core", core)

    def _root(self) -> Expr:
        core = object.__getattribute__(self, "_core")
        meta = object.__getattribute__(self, "_meta")
        return Expr(Plan(root="Document", source={"document_id": meta["id"]}), core)

    @property
    def lazy(self) -> Expr:
        """A recorder rooted at this remote document -- batch a chain of ops into
        one round-trip: ``d.lazy.select(...).text_content.collect()``."""
        return self._root()

    def collect(self, context: Any = None) -> "_RemoteDoc":
        """An eager handle is already materialised (parity with ``WebCore``)."""
        return self

    def _wrap(self, value: Any) -> Any:
        from ...collection import Collection, Field

        if isinstance(value, Field):
            return value.get()
        if isinstance(value, list) and value and isinstance(value[0], _RemoteDoc):
            core = object.__getattribute__(self, "_core")
            return Collection(value, client=core, root=self._meta["id"])
        return value

    def __getattr__(self, name: str) -> Any:
        meta = object.__getattribute__(self, "_meta")
        if name in meta:  # title / ok / kind / id -- inline, no round-trip
            return meta[name]
        if name.startswith("_"):
            raise AttributeError(name)
        from ..document import DocumentCore

        if name in DocumentCore.prop_ops():  # a value attr -> round-trip its value
            return self._wrap(getattr(self._root(), name).collect())
        if name in DocumentCore.ops():  # a call op -> round-trip on call

            def _call(*args: Any, **kwargs: Any) -> Any:
                kwargs.pop("_collect", None)
                return self._wrap(getattr(self._root(), name)(*args, **kwargs).collect())

            return _call
        raise AttributeError(name)

    def __repr__(self) -> str:
        return f"_RemoteDoc({object.__getattribute__(self, '_meta')})"


class RemoteWebClientCore(WebClientCore):
    """A ``WebClientCore`` whose ``execute`` round-trips to ``/execute`` instead
    of running locally. The authoring backing (fetch) is inherited, so the
    surface is unchanged; only execution differs."""

    url: str
    token: str | None = None

    _http: Any = PrivateAttr(default=None)

    def model_post_init(self, ctx: Any) -> None:
        super().model_post_init(ctx)
        self.url = self.url.rstrip("/")
        # bound every round-trip by the client's timeout so a hung service can't
        # block the caller forever.
        self._http = httpx.Client(timeout=self.timeout)

    def _init_transport(self) -> None:
        """No local transport pool -- execution is a remote round-trip."""

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    # -- eager client verbs: the remote dispatcher -----------------------------
    # The remote core has no local engine, so it cannot dispatch the FetchBacking
    # ops in-process (there is nothing to fetch with). Instead each client verb is
    # wrapped into a one-step ``WebClient`` plan and executed over the wire -- the
    # "convert anything that is not a plan into a plan, then run it remotely" rule.
    # ``fetch`` / ``summary`` resolve eagerly (one round-trip -> a ``_RemoteDoc``
    # handle / a value); ``ref`` stays a lazy ``Expr`` so a portable plan can be
    # collected against it (``plan.collect(rc.ref(url))``). Batch a doc's ops with
    # ``rc.lazy`` (one plan, one round-trip) instead of per-op.
    def _verb(self) -> Any:
        return Expr(Plan(root="WebClient"), self)

    def fetch(self, url: Any, **kw: Any) -> Any:
        return self._verb().fetch(url, **kw).collect()

    def summary(self, url: Any, *include: str, **kw: Any) -> Any:
        return self._verb().summary(url, *include, **kw).collect()

    def ref(self, url: Any, method: str = "get", **kw: Any) -> Any:
        return self._verb().ref(url, method, **kw)

    # -- execution: one Plan POSTed to /execute ------------------------------
    def execute(self, expr: Any, context: Any = None, *, stream: bool = False) -> Any:
        body: dict[str, Any] = {"plan": expr._plan.model_dump()}
        src = expr._plan.source
        if src and "document_id" in src:
            body["document_id"] = src["document_id"]
        elif src:  # a reference(url)-rooted plan carries its spec
            body["url"] = _url_of(src)
        # a context roots a context-based plan (e.g. ref.resolve()) server-side
        if isinstance(context, _RemoteDoc):
            body["document_id"] = context._meta["id"]
        elif isinstance(context, Expr):  # a client ref/fetch -- send its plan
            body["context_plan"] = context._plan.model_dump()
        resp = self._http.post(
            f"{self.url}/execute", json=body, headers=self._headers()
        )
        if not (200 <= resp.status_code < 300):
            from ...errors import RemoteError, WebError

            err: WebError | None = None
            try:  # the service sends {"error": {type, message, status_code, ...}}
                payload = resp.json()
                if isinstance(payload, dict) and isinstance(payload.get("error"), dict):
                    err = WebError(**payload["error"])
            except Exception:
                pass
            raise RemoteError(resp.status_code, resp.text[:200], error=err)
        # the service returns clean JSON (raw scalars); wrap a scalar leaf back
        # into a Field just as a local ``execute`` does, so remote and local
        # ``collect()`` agree on the result type.
        return _materialize(self._deserialize(resp.json()["rows"]))

    def _deserialize(self, rows: Any) -> Any:
        if isinstance(rows, dict) and "__doc__" in rows:
            return _RemoteDoc(rows["__doc__"], self)
        if isinstance(rows, list):
            return [self._deserialize(r) for r in rows]
        return rows

    def close(self) -> None:
        if self._http is not None:
            self._http.close()
        super().close()

    # -- server-side sessions ------------------------------------------------
    def session(self, *, ttl: float | None = None, **kw: Any) -> "RemoteSession":
        resp = self._http.post(
            f"{self.url}/sessions", json={"ttl": ttl}, headers=self._headers()
        )
        resp.raise_for_status()
        return RemoteSession(self, resp.json()["id"])

    def close_session(self, sid: str) -> None:
        self._http.delete(f"{self.url}/sessions/{sid}", headers=self._headers())


class RemoteSession:
    """A handle to a server-side session; its fetches thread the session id into
    the plan so the server resolves them through that session. A context manager
    (``with rc.session() as s:``) so the server-side session is always closed."""

    def __init__(self, core: RemoteWebClientCore, sid: str) -> None:
        self._core = core
        self._id = sid
        self._status = "running"

    def ref(self, url: str, method: str = "get", **kw: Any) -> Any:
        spec = _core_from_url(url, cast(HttpMethod, method), **kw).model_dump()
        return Expr(
            Plan(root="Reference", source=spec, session_id=self._id), self._core
        )

    def fetch(self, url: str, **kw: Any) -> Any:
        return self.ref(url, **kw).resolve()

    def close(self) -> None:
        self._core.close_session(self._id)
        self._status = "closed"

    def __enter__(self) -> "RemoteSession":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @property
    def id(self) -> str:
        return self._id

    @property
    def status(self) -> str:
        return self._status


__all__ = ["RemoteWebClientCore", "RemoteSession"]
