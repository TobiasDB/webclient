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


RAISE: _Policy = _Policy("RAISE")     # default: a failed fetch/resolve raises
RETURN: _Policy = _Policy("RETURN")   # lenient: return a not-ok document


class WebError(BaseModel):
    """A serializable failure attached to a not-ok document."""

    type: str = "http_error"
    message: str = ""
    status_code: int = 0


class WebException(Exception):
    """Raised by a loud (RAISE) fetch/resolve; carries the ``WebError``."""

    def __init__(self, error: WebError) -> None:
        super().__init__(error.message or f"HTTP {error.status_code}")
        self.error = error


def error_for(status_code: int, message: str = "") -> WebError:
    """Classify an HTTP status into a ``WebError``."""
    if status_code == 404:
        kind = "not_found"
    elif status_code == 0:
        kind = "transport_error"
    elif 500 <= status_code < 600:
        kind = "server_error"
    elif 400 <= status_code < 500:
        kind = "client_error"
    else:
        kind = "http_error"
    return WebError(type=kind, status_code=status_code,
                    message=message or f"{status_code} for the request")


__all__ = ["RAISE", "RETURN", "WebError", "WebException", "error_for"]
