"""Typed errors. Every failure mode the interface promises has a class here,
so callers never have to match on message text."""
from __future__ import annotations

from typing import Any


class WebClientError(Exception):
    """Base for everything this package raises deliberately."""


class UnsupportedOperation(WebClientError):
    """An op was called on a Document whose backing cannot serve it.

    Capability is *runtime* state: a Document whose page moved on keeps its
    static half and loses its live half, so this can fire on an object that
    supported the same op a moment ago.
    """

    def __init__(self, op: str, capability: str, backing: str,
                 hint: str | None = None) -> None:
        message = (f"{op}() requires backing capability {capability!r}; "
                   f"this Document has a {backing} backing.")
        if hint:
            message += " " + hint
        super().__init__(message)
        self.op, self.capability, self.backing = op, capability, backing


class StaleDocument(WebClientError):
    """A live op was called on a Document whose page has moved on. The static
    half (content, render, telemetry) still works."""


class ResolveError(WebClientError):
    """A reference could not be resolved: transport failure, or a non-2xx
    status. Carries the Document when a response was received."""

    def __init__(self, message: str, document: Any = None,
                 status_code: int = 0) -> None:
        super().__init__(message)
        self.document = document
        self.status_code = status_code


class SelectionError(LookupError, WebClientError):
    """No element matched, or a requested attribute is absent."""


class PlanError(WebClientError):
    """A plan is malformed: unknown op, wrong cardinality, unnamed field.
    Always raised at compile time, never silently skipped at run time."""
