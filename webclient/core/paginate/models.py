"""Pagination's model + interface (crawl's twin, for a paginated series).

``IPagination`` is a pagination walk's state: its :class:`PaginationConfig` (the advance +
the stops) and its live state -- the ``pages`` fetched so far (resolved :class:`Document`\\ s,
so their content is an expression), the running ``rows_seen``, and the terminal
:class:`PaginationVerdict` once it stops. ``Pagination`` inherits it and holds the walk
machinery (the advance state + the :class:`~webclient.loop.BoundedLoop` that drives it).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel

from ...loop import LoopVerdict

if TYPE_CHECKING:
    from . import Pagination  # noqa: F401  (step/run return the pagination itself)


class PaginationConfig(BaseModel):
    """How to walk a paginated series: the ADVANCE (how the next page is reached) and the STOPS
    (when to end). Assembled by ``wc.paginate(source, **kwargs)`` from keyword args, or built
    explicitly. Mirrors ``doc.paginate(...)``'s kwargs, so the manual session and the bound op
    walk a source the same way."""

    by: str = "auto"  # "auto" (from the page's pagination hint) | "link" | "param" | "cursor"
    max_pages: int = 20  # the page budget
    max_rows: int = 0  # stop once this many records are seen (needs ``records``); 0 = unlimited
    name: str = "page"  # the query param the param/cursor advance writes
    start: int = 1  # param advance: the first page number
    step: int = 1  # param advance: the increment
    size: int = 0  # param advance: an offset stride (``?offset=`` style) instead of a page number
    cursor: str = ""  # cursor advance: the selector reading the next-cursor token off each page
    cursor_attr: str = "text"  # cursor advance: the attribute (or "text") holding the token
    records: str = ""  # a record selector -- lets the walk count rows (for ``max_rows``)
    until: str = ""  # recency stop: a per-record ordering field (e.g. a date selector)
    until_before: str = ""  # recency stop: end once ``until``'s oldest value sorts below this


class PaginationVerdict(LoopVerdict):
    """Why a pagination walk stopped, plus its yield. Extends :class:`~webclient.loop.LoopVerdict`
    (``done`` / ``reason`` / ``rounds`` / ``error``) with ``pages`` and ``rows`` collected and the
    precise ``stop`` cause: ``no-next`` / ``empty`` (the series ended), ``cutoff`` (the recency
    ``until`` was reached), ``rows`` / ``budget`` (a bound was hit), ``clamp`` (a page repeated an
    earlier one -- an out-of-range clamp, mapped to ``reason="stalled"``), or ``error``."""

    pages: int = 0
    rows: int = 0
    stop: str = ""


class IPagination(BaseModel):
    """A paginated walk's state: its :class:`PaginationConfig` + the live ``pages`` / ``rows_seen``
    / ``verdict``, plus (for the checker) the ops ``Pagination`` implements. The ops are
    ``TYPE_CHECKING``-only, so at runtime this is just the state model. ``status`` (running /
    closed / expired) is the shared session lifecycle from ``SessionCore``."""

    model_config = {"arbitrary_types_allowed": True}

    config: PaginationConfig = PaginationConfig()
    #: the pages fetched so far, page one first -- resolved :class:`Document`\\ s (an expression:
    #: ``pages[0].select_all(...)``). ``Any`` so pydantic never validates/copies a live Document.
    pages: list[Any] = []
    rows_seen: int = 0  # records counted across the fetched pages (when ``records`` is set)
    verdict: PaginationVerdict | None = None  # why it stopped (once terminal)

    def __str__(self) -> str:
        """A short digest: status, pages walked, rows seen, and the stop cause when finished."""
        status = getattr(self, "status", "running")
        head = f"pagination [{status}] · {len(self.pages)} page(s), {self.rows_seen} row(s)"
        if self.verdict is not None:
            head += f" · stopped: {self.verdict.stop or self.verdict.reason}"
        urls = [getattr(p, "final_url", None) or getattr(p, "url", "?") for p in self.pages[:8]]
        lines = [head] + [f"  {u}" for u in urls]
        if len(self.pages) > 8:
            lines.append(f"  … +{len(self.pages) - 8} more")
        return "\n".join(lines)

    if TYPE_CHECKING:
        # the ops Pagination implements (hand-declared, like the eager Document row ops -- this
        # SessionCore's surface is small and stable, so it is not gen_stubs-generated).
        @property
        def done(self) -> bool: ...
        def step(self) -> "Pagination": ...
        def run(self) -> "Pagination": ...


__all__ = ["PaginationConfig", "PaginationVerdict", "IPagination"]
