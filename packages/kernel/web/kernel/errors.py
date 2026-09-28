"""Structured errors: a failure is data first, an exception second.

A :class:`WebError` is a small, serialisable value -- a stable ``code``, a human ``message``,
and free-form ``detail``. It travels on results (a not-ok Snapshot/Document carries one) and
on the bus, so a caller can inspect a failure without catching anything. :class:`WebException`
is the exception form, raised only when a layer chooses to fail loudly; it carries the same
``WebError`` on ``.error``. The kernel defines no error *catalog* -- each layer names its own
codes (``"http.timeout"``, ``"parse.not_html"``, ...). That is the whole error model.
"""

from __future__ import annotations

from pydantic import BaseModel, JsonValue


class WebError(BaseModel):
    """A structured, serialisable error. ``code`` is a stable dotted identifier a caller can
    branch on; ``message`` is for humans; ``detail`` carries anything else (status, url, ...)."""

    code: str
    message: str = ""
    detail: dict[str, JsonValue] = {}

    def __str__(self) -> str:
        return f"{self.code}: {self.message}" if self.message else self.code


class WebException(Exception):
    """The exception form of a :class:`WebError` -- raised when a layer fails loudly rather
    than returning a not-ok value. One ``except WebException`` catches any layer's failure and
    reads its structured ``.error``."""

    def __init__(self, error: "WebError | str", message: str = "") -> None:
        self.error = WebError(code=error, message=message) if isinstance(error, str) else error
        super().__init__(str(self.error))


def err(code: str, message: str = "", **detail: JsonValue) -> WebError:
    """Build a :class:`WebError` concisely: ``err("http.timeout", "no response", url=u)``."""
    return WebError(code=code, message=message, detail=detail)


__all__ = ["WebError", "WebException", "err"]
