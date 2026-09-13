"""Error policy: the ``WebError`` value + the RAISE/RETURN policy markers.

A failed fetch/resolve is loud by default (RAISE); ``error=RETURN`` (or
``optional=True``) instead yields a not-ok ``Document`` carrying a serializable
``WebError``. Value ops (``is_ok`` / ``is_empty`` / ``error`` / ``message``)
work on a not-ok document too.
"""

from __future__ import annotations

from pydantic import BaseModel


class _Policy:
    """A named error-policy marker (identity-compared)."""

    __slots__ = ("name",)

    def __init__(self, name: str) -> None:
        self.name = name

    def __repr__(self) -> str:
        return self.name


RAISE: _Policy = _Policy("RAISE")  # default: a failed fetch/resolve raises
RETURN: _Policy = _Policy("RETURN")  # lenient: return a not-ok document

import contextlib as _contextlib
import contextvars as _contextvars

#: the ambient error policy: RAISE at the top level; extract/filter run their
#: sub-expressions under RETURN so one bad field never aborts a whole plan.
_CURRENT: "_contextvars.ContextVar[_Policy]" = _contextvars.ContextVar(
    "webclient_policy", default=RAISE
)


def current_policy() -> "_Policy":
    return _CURRENT.get()


@_contextlib.contextmanager
def default_policy(policy: "_Policy"):
    token = _CURRENT.set(policy)
    try:
        yield
    finally:
        _CURRENT.reset(token)


class WebError(BaseModel):
    """A serializable failure attached to a not-ok document."""

    type: str = "http_error"
    message: str = ""
    status_code: int = 0


class WebException(Exception):
    """Raised by a loud (RAISE) fetch/resolve; carries the ``WebError`` and the
    not-ok ``document`` (when one was built)."""

    def __init__(self, error: WebError, document: object | None = None) -> None:
        super().__init__(error.message or f"HTTP {error.status_code}")
        self.error = error
        self.document = document


#: a fetch/resolve failure -- the name used at the call sites/tests.
FetchError = WebException


class RemoteError(Exception):
    """A remote ``/execute`` call returned a non-2xx response."""

    def __init__(self, status_code: int, detail: str = "") -> None:
        super().__init__(f"remote execute failed: {status_code} {detail}".strip())
        self.status_code = status_code


def error_for(status_code: int, message: str = "") -> WebError:
    """Classify an HTTP status into a ``WebError``."""
    kind = "TransportError" if status_code == 0 else "HTTPStatus"
    return WebError(
        type=kind,
        status_code=status_code,
        message=message or f"HTTP {status_code} for the request",
    )


__all__ = [
    "RAISE",
    "RETURN",
    "WebError",
    "WebException",
    "FetchError",
    "RemoteError",
    "error_for",
    "current_policy",
    "default_policy",
]
