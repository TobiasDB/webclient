"""HAR (HTTP Archive) support for the ``"har"`` replay mode: build a HAR from a trace's
network events, load one from disk, and serve it back as an httpx transport so a
recorded plan re-executes with ZERO network calls (the static tier's twin of Playwright's
``route_from_har``, which the browser tier uses directly).

A HAR entry is matched by ``(method, url)``; a POST also matches on its body. A request
with no entry gets a ``599`` response carrying ``x-webclient-har: miss`` -- a not-ok
document (never a real fetch), so a replay that drifts from its recording is visible.
"""

from __future__ import annotations

import base64
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast

import httpx

__all__ = ["har_from_events", "load_har", "save_har", "HarTransport", "HAR_MISS"]

HAR_MISS = 599


def _entry(method: str, url: str, status: int, headers: dict[str, str], body: bytes | None,
           elapsed: float | None, started: float | None, request_body: bytes | None = None) -> dict[str, Any]:
    """One HAR ``entries[]`` record (the subset the transport reads back)."""
    text: str
    encoding: str | None = None
    try:
        text = (body or b"").decode("utf-8")
    except UnicodeDecodeError:
        text = base64.b64encode(body or b"").decode("ascii")
        encoding = "base64"
    content: dict[str, Any] = {"size": len(body or b""), "mimeType": headers.get("content-type", ""), "text": text}
    if encoding:
        content["encoding"] = encoding
    when = datetime.fromtimestamp(started or 0, tz=timezone.utc).isoformat() if started else ""
    entry: dict[str, Any] = {
        "startedDateTime": when,
        "time": round((elapsed or 0.0) * 1000, 1),
        "request": {"method": method.upper(), "url": url, "headers": [], "queryString": [],
                    "headersSize": -1, "bodySize": len(request_body or b"")},
        "response": {"status": status, "statusText": "", "httpVersion": "HTTP/1.1",
                     "headers": [{"name": k, "value": v} for k, v in headers.items()],
                     "content": content, "redirectURL": headers.get("location", ""),
                     "headersSize": -1, "bodySize": len(body or b"")},
        "cache": {}, "timings": {"send": 0, "wait": round((elapsed or 0.0) * 1000, 1), "receive": 0},
    }
    if request_body:
        entry["request"]["postData"] = {"mimeType": "", "text": request_body.decode("utf-8", "replace")}
    return entry


def har_from_events(events: "list[Any]") -> dict[str, Any]:
    """A HAR ``log`` built from the trace's network events that captured a body (the static
    tier's navigations under tracing). Events without a body are skipped -- there is nothing
    to serve back."""
    entries: list[dict[str, Any]] = []
    for e in events:
        if not str(getattr(e, "topic", "")).startswith("network"):
            continue
        body = getattr(e, "body", None)
        if body is None:
            continue
        req = getattr(e, "request", None)
        url = ""
        method = getattr(e, "method", None) or "GET"
        if req is not None:
            try:
                url = str(req.dispatch("url"))
                method = str(getattr(req, "method", method) or method)
            except Exception:  # noqa: BLE001 - an unbound reference: use what it has
                url = str(getattr(req, "url", "") or "")
        if not url:
            continue
        entries.append(_entry(
            method, url, int(getattr(e, "status_code", 0) or 0), dict(getattr(e, "headers", None) or {}),
            bytes(body), getattr(e, "elapsed", None), getattr(e, "ts", None),
        ))
    return {"log": {"version": "1.2", "creator": {"name": "webclient", "version": "0.1.0"},
                    "entries": entries}}


def save_har(har: dict[str, Any], path: "str | Path") -> Path:
    """Write a HAR dict to ``path`` (JSON), creating parent directories."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(har, indent=1))
    return p


def load_har(path: "str | Path") -> dict[str, Any]:
    """Read a HAR file (ours or Playwright's) into its ``log`` dict."""
    data: dict[str, Any] = json.loads(Path(path).read_text())
    return cast("dict[str, Any]", data["log"] if "log" in data else data)


class HarTransport(httpx.AsyncBaseTransport):
    """An httpx transport that answers from a HAR instead of the network. ``strict``
    (default) matches ``(method, url)`` exactly; otherwise the query string is ignored as
    a fallback. A miss returns :data:`HAR_MISS` with ``x-webclient-har: miss``."""

    def __init__(self, har: "dict[str, Any] | str | Path", *, strict: bool = True) -> None:
        log = load_har(har) if isinstance(har, (str, Path)) else (har.get("log", har))
        self.entries: list[dict[str, Any]] = list(log.get("entries", []))
        self.strict = strict
        self.hits = 0
        self.misses: list[str] = []
        self._by_key: dict[tuple[str, str], dict[str, Any]] = {}
        for e in self.entries:
            r = e.get("request", {})
            key = (str(r.get("method", "GET")).upper(), str(r.get("url", "")))
            self._by_key.setdefault(key, e)  # first recording wins

    def _find(self, method: str, url: str) -> "dict[str, Any] | None":
        entry = self._by_key.get((method, url))
        if entry is None and not self.strict:
            bare = url.split("?", 1)[0]
            for (m, u), e in self._by_key.items():
                if m == method and u.split("?", 1)[0] == bare:
                    return e
        return entry

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        entry = self._find(request.method.upper(), str(request.url))
        if entry is None:
            self.misses.append(f"{request.method} {request.url}")
            return httpx.Response(
                HAR_MISS, headers={"x-webclient-har": "miss"},
                content=b"", request=request,
            )
        self.hits += 1
        resp = entry["response"]
        content = resp.get("content", {})
        text = content.get("text", "") or ""
        body = base64.b64decode(text) if content.get("encoding") == "base64" else text.encode("utf-8")
        headers = [(h["name"], h["value"]) for h in resp.get("headers", [])
                   if h["name"].lower() not in ("content-encoding", "transfer-encoding", "content-length")]
        return httpx.Response(int(resp.get("status", 200)), headers=headers, content=body, request=request)
