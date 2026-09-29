"""Structured errors + failure-mode classification.

A :class:`WebError` is a small, serialisable value -- a stable ``code``, a human ``message`` and
free-form ``detail`` -- that travels on results (a not-ok Snapshot/Document carries one) and on the
bus, so a caller inspects a failure without catching. :class:`WebException` is its exception form.
No error *catalog* is defined here -- each layer names its own codes (``"http.timeout"``,
``"parse.not_html"``, ...). These are the shared base types (they lived in the removed ``web.kernel``
bottom layer; fetch is now the lowest shared layer, so they live here).

:func:`classify` then maps a raw backend error into a stable taxonomy.

A fetch backend must never leak an ``httpx`` or ``playwright`` exception upward; it returns a
Snapshot carrying a structured :class:`WebError`. But a single ``fetch.transport``
blob hides *why* it failed, and failure modes are what a caller (or the resolve escalation
policy) branches on. :func:`classify` maps the raw exception to one stable code:

    fetch.timeout     the request timed out (connect / read / navigation)
    fetch.dns         the host could not be resolved
    fetch.connect     the connection was refused / reset / unreachable
    fetch.tls         a TLS / certificate failure
    fetch.proxy       the proxy failed
    fetch.redirects   too many redirects
    fetch.url         a malformed / unsupported URL or scheme
    fetch.protocol    a malformed response (bad framing)
    fetch.aborted     the request was aborted (browser)
    fetch.browser     another navigation / browser error
    fetch.transport   unclassified (the fallback)

The code is stable across backends: an HTTP timeout and a browser navigation timeout are both
``fetch.timeout``, so policy can be written once. ``detail`` keeps the url and the original
exception type for debugging.
"""

from __future__ import annotations

import socket
import ssl

import httpx
from pydantic import BaseModel, JsonValue


class WebError(BaseModel):
    """A structured, serialisable error. ``code`` is a stable dotted identifier a caller can branch
    on; ``message`` is for humans; ``detail`` carries anything else (status, url, ...)."""

    code: str
    message: str = ""
    detail: dict[str, JsonValue] = {}

    def __str__(self) -> str:
        return f"{self.code}: {self.message}" if self.message else self.code


class WebException(Exception):
    """The exception form of a :class:`WebError` -- raised when a layer fails loudly rather than
    returning a not-ok value. One ``except WebException`` catches any layer's failure and reads its
    structured ``.error``."""

    def __init__(self, error: "WebError | str", message: str = "") -> None:
        self.error = WebError(code=error, message=message) if isinstance(error, str) else error
        super().__init__(str(self.error))


def err(code: str, message: str = "", **detail: JsonValue) -> WebError:
    """Build a :class:`WebError` concisely: ``err("http.timeout", "no response", url=u)``."""
    return WebError(code=code, message=message, detail=detail)


def _causes(exc: BaseException) -> list[BaseException]:
    """The exception plus its ``__cause__`` / ``__context__`` chain (deduped, bounded)."""
    out: list[BaseException] = []
    cur: "BaseException | None" = exc
    while cur is not None and cur not in out and len(out) < 10:
        out.append(cur)
        cur = cur.__cause__ or cur.__context__
    return out


def _playwright_code(exc: BaseException) -> "str | None":
    """Classify a playwright error by type name + Chromium ``net::`` code, without importing
    playwright (an optional dependency)."""
    if not type(exc).__module__.startswith("playwright"):
        return None
    if type(exc).__name__ == "TimeoutError":
        return "fetch.timeout"
    m = str(exc)
    if "ERR_NAME_NOT_RESOLVED" in m:
        return "fetch.dns"
    if "ERR_CERT" in m or "ERR_SSL" in m:
        return "fetch.tls"
    if "ERR_ABORTED" in m:
        return "fetch.aborted"
    if ("ERR_CONNECTION" in m or "ERR_ADDRESS_UNREACHABLE" in m or "ERR_INTERNET_DISCONNECTED" in m
            or "ERR_UNSAFE_PORT" in m):
        return "fetch.connect"
    return "fetch.browser"


def classify(exc: BaseException, *, url: str = "") -> WebError:
    """Map a raw transport exception (httpx / playwright / stdlib) to a stable, structured
    :class:`WebError` (see the module docstring for the taxonomy)."""
    causes = _causes(exc)
    code: "str | None" = None

    if isinstance(exc, httpx.TimeoutException):
        code = "fetch.timeout"
    elif isinstance(exc, httpx.ProxyError):
        code = "fetch.proxy"
    elif isinstance(exc, httpx.TooManyRedirects):
        code = "fetch.redirects"
    elif isinstance(exc, (httpx.UnsupportedProtocol, httpx.InvalidURL)):
        code = "fetch.url"
    elif isinstance(exc, httpx.RemoteProtocolError):
        code = "fetch.protocol"
    elif isinstance(exc, httpx.ConnectError):
        if any(isinstance(c, ssl.SSLError) for c in causes):
            code = "fetch.tls"
        elif any(isinstance(c, socket.gaierror) for c in causes):
            code = "fetch.dns"
        else:
            code = "fetch.connect"
    elif isinstance(exc, httpx.NetworkError):  # Read/Write/Close errors (base after ConnectError)
        code = "fetch.connect"
    elif any(isinstance(c, ssl.SSLError) for c in causes):
        code = "fetch.tls"

    if code is None:
        code = _playwright_code(exc) or "fetch.transport"
    return WebError(code=code, message=str(exc) or type(exc).__name__,
                    detail={"url": url, "exc": type(exc).__name__})


__all__ = ["WebError", "WebException", "err", "classify"]
