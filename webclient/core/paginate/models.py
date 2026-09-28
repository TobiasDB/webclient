"""Pagination's model + interface (crawl's twin, for a paginated series).

``IPagination`` is a pager session's state: its :class:`PaginationConfig` (the ONE iterator + until /
filter / bounds -- the same config ``doc.paginate(...)`` takes) and its live state -- the kept ``pages``
so far (resolved :class:`Document`\\ s, so their content is an expression), ``rows_seen`` and the
terminal :class:`PaginationVerdict` once it stops.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from ...kernel.loop import LoopVerdict
from ..document.paginate import PaginationConfig

if TYPE_CHECKING:
    from . import Pagination  # noqa: F401  (step/run return the pagination itself)


class PaginationVerdict(LoopVerdict):
    """Why a pager stopped, plus its yield. Extends :class:`~webclient.loop.LoopVerdict` with the kept
    ``pages``, the ``fetched`` count, the ``rows`` seen (with ``records``) and the ``stop`` cause:
    ``end`` (no next link / token, or ``stop`` reached), ``empty`` (a page came back empty or failed),
    ``repeat`` (a page repeated an earlier one -- an out-of-range clamp), ``until`` (``until`` held),
    ``exhausted`` (a click / scroll loaded nothing more), ``budget`` (``max_pages``), or ``error``."""

    pages: int = 0
    fetched: int = 0
    rows: int = 0
    stop: str = ""


class IPagination(BaseModel):
    """A pager session's state: its :class:`PaginationConfig` + the live ``pages`` / ``rows_seen`` /
    ``verdict``, plus (for the checker) the ops ``Pagination`` implements. ``status`` (running / closed
    / expired) is the shared session lifecycle from ``SessionCore``."""

    model_config = {"arbitrary_types_allowed": True}

    config: PaginationConfig = PaginationConfig(pages="page")
    #: the KEPT pages so far, page one first -- resolved :class:`Document`\\ s (an expression:
    #: ``pages[0].select_all(...)``). ``Any`` so pydantic never validates/copies a live Document.
    pages: list[Any] = []
    rows_seen: int = 0  # records counted across the kept pages (when ``records`` is set)
    verdict: PaginationVerdict | None = None  # why it stopped (once terminal)

    def __str__(self) -> str:
        """A short digest: status, pages kept, rows seen, and the stop cause when finished."""
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
        # the ops Pagination implements (hand-declared: this SessionCore's surface is small and stable).
        @property
        def done(self) -> bool: ...
        def step(self) -> "Pagination": ...
        def run(self) -> "Pagination": ...


__all__ = ["PaginationConfig", "PaginationVerdict", "IPagination"]
