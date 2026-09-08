"""RemoteWebClient: drive a webclient service over HTTP, no local browser.

Mirrors the WebClient facade but every operation is a request to the service
(create_app). Needs only httpx + pydantic -- no lxml, no playwright, no
browser binaries -- because all fetching, browsing and parsing happen
server-side and documents cross the wire as handles.

    rc = RemoteWebClient("https://host", token="secret")
    doc = rc.ref("https://example.com").fetch()   # a RemoteDocument handle
    print(doc.render("markdown"))
    plan = q.ref.fetch().select_all(".card").map(title=q.node.select(".title").text)
    rows = plan.collect(rc.ref("https://example.com"))   # runs server-side

Portability contract (ISSUES #37): the lazy/plan API (`q` + collect/execute)
is 100% portable -- identical results local or remote. The imperative
document surface is best-effort and chatty: render / one-level select / text
/ title work; deep nested selection lowers to a plan.
"""
from __future__ import annotations

from typing import Any, Iterator, Sequence

import httpx

from .lazy.expr import Expr, QueryPlan
from .models import Reference


class RemoteError(RuntimeError):
    """A service call failed (non-2xx from the remote API)."""


class RemoteWebClient:
    """Facade-compatible client backed by a webclient service."""

    def __init__(self, base_url: str, *, token: str | None = None,
                 transport: Any = None, timeout: float = 60.0) -> None:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        # transport= lets tests point at an in-process ASGI app.
        self._http = httpx.Client(base_url=base_url.rstrip("/"),
                                  headers=headers, timeout=timeout,
                                  transport=transport)

    # -- lifecycle -----------------------------------------------------------
    def __enter__(self) -> "RemoteWebClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    def _call(self, method: str, path: str, **kw: Any) -> Any:
        resp = self._http.request(method, path, **kw)
        if resp.status_code >= 400:
            raise RemoteError(f"{method} {path} -> {resp.status_code}: "
                              f"{resp.text[:200]}")
        return resp.json()

    # -- sessions ------------------------------------------------------------
    def session(self, *, ttl: float | None = None, keep_alive: bool = False,
                headers: dict[str, str] | None = None) -> "RemoteSession":
        data = self._call("POST", "/sessions", json={
            "ttl": ttl, "keep_alive": keep_alive, "headers": headers or {}})
        return RemoteSession(self, data["id"], data)

    # -- references / fetching ----------------------------------------------
    def ref(self, url: str, method: str = "get", **kwargs: Any) -> "RemoteRef":
        return RemoteRef(self, url, method, kwargs.get("headers", {}))

    def fetch(self, ref: "RemoteRef | Reference | str", *, browser: bool = False,
              session: "RemoteSession | None" = None,
              optional: bool = False, **_: Any) -> "RemoteDocument":
        url = ref.url if isinstance(ref, (RemoteRef, Reference)) else ref
        method = getattr(ref, "method", "get")
        headers = getattr(ref, "headers", {}) or {}
        session_id = session.id if session else getattr(ref, "session_id", None)
        meta = self._call("POST", "/fetch", json={
            "url": url, "method": method, "browser": browser,
            "headers": headers, "optional": optional,
            "session_id": session_id})
        return RemoteDocument(self, meta["id"], meta)

    def document(self, document_id: str) -> "RemoteDocument":
        meta = self._call("GET", f"/documents/{document_id}")
        return RemoteDocument(self, document_id, meta)

    # -- plan execution (fully portable) -------------------------------------
    def execute(self, plan: Expr | QueryPlan, context: Any, *,
                stream: bool = False) -> Any:
        query = plan.to_query() if isinstance(plan, Expr) else plan
        url = context.url if isinstance(context, (RemoteRef, Reference)) else str(context)
        session_id = getattr(context, "session_id", None) or getattr(
            getattr(context, "_session", None), "id", None)
        params: dict[str, Any] = {"url": url}
        if session_id:
            params["session_id"] = session_id
        result = self._call("POST", "/plans", params=params,
                            json=query.model_dump())
        rows = result["rows"]
        return iter(rows) if stream else rows


class RemoteRef:
    """A remote reference: a URL the service will fetch. Mirrors the Reference
    fetch surface so `rc.ref(url).fetch()` works like the local client."""

    def __init__(self, client: RemoteWebClient, url: str, method: str,
                 headers: dict[str, str]) -> None:
        self._client = client
        self.url = url
        self.method = method
        self.headers = headers
        self.session_id: str | None = None

    def fetch(self, *, browser: bool = False,
              session: "RemoteSession | None" = None,
              optional: bool = False, **_: Any) -> "RemoteDocument":
        return self._client.fetch(self, browser=browser, session=session,
                                  optional=optional)


class RemoteSession:
    def __init__(self, client: RemoteWebClient, id: str,
                 meta: dict[str, Any]) -> None:
        self._client = client
        self.id = id
        self.status = meta.get("status", "running")
        self.expires_at = meta.get("expires_at")

    def ref(self, url: str, method: str = "get", **kwargs: Any) -> RemoteRef:
        ref = self._client.ref(url, method, **kwargs)
        ref.session_id = self.id
        return ref

    def refresh(self) -> "RemoteSession":
        meta = self._client._call("GET", f"/sessions/{self.id}")
        self.status = meta["status"]
        return self

    def close(self) -> None:
        meta = self._client._call("DELETE", f"/sessions/{self.id}")
        self.status = meta["status"]


class RemoteDocument:
    """A server-side document, referenced by id. Rich operations lower to
    service calls; nested/complex extraction should use a plan (execute)."""

    def __init__(self, client: RemoteWebClient, id: str,
                 meta: dict[str, Any]) -> None:
        self._client = client
        self.id = id
        self.kind = meta.get("kind")
        self.status_code = meta.get("status_code")
        self.final_url = meta.get("final_url")
        self.session_id = meta.get("session_id")
        self._title = meta.get("title")

    @property
    def ok(self) -> bool:
        return bool(self.status_code and 200 <= self.status_code < 300)

    @property
    def title(self) -> str | None:
        return self._title

    # -- representations -----------------------------------------------------
    def render(self, format: str = "markdown", *,
               main_content_only: bool = False) -> Any:
        result = self._client._call(
            "GET", f"/documents/{self.id}/render",
            params={"format": format, "main_content_only": main_content_only})
        return result["result"]

    @property
    def markdown(self) -> str:
        return self.render("markdown")

    @property
    def text(self) -> str:
        return self.render("text")

    @property
    def elements(self) -> list[dict[str, Any]]:
        return self.render("elements")

    def links(self) -> list[str]:
        return self.render("links")

    # -- one-level selection (deeper extraction -> use a plan) ---------------
    def select(self, selector: str, *, attr: str | None = None) -> Any:
        result = self._client._call(
            "POST", f"/documents/{self.id}/select",
            json={"selector": selector, "all": False, "attr": attr})
        return result["value"]

    def select_all(self, selector: str, *,
                   attr: str | None = None) -> list[Any]:
        result = self._client._call(
            "POST", f"/documents/{self.id}/select",
            json={"selector": selector, "all": True, "attr": attr})
        return result["values"]

    def __repr__(self) -> str:
        return f"RemoteDocument(id={self.id!r}, kind={self.kind!r})"
