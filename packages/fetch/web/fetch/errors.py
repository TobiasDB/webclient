"""Failure modes -- classify raw backend errors into a clean, stable taxonomy.

A fetch backend must never leak an ``httpx`` or ``playwright`` exception upward; it returns a
Snapshot carrying a structured :class:`~web.kernel.WebError`. But a single ``fetch.transport``
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

from web.kernel import WebError


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
    :class:`~web.kernel.WebError` (see the module docstring for the taxonomy)."""
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


__all__ = ["classify"]
