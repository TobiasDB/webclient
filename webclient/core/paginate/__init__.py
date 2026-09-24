"""Pagination: a stateful walk over a paginated series -- crawl's twin, for one dataset's pages.

Created by ``WebClient.paginate(source, ...)``. Drive it two ways over the one step-engine::

    with wc.paginate("https://site/list?page=1", by="param", name="page") as pg:
        pg.run()                       # batch: walk to the end, read .pages
        # or manual:  pg.step()        # fetch one more page; inspect .pages / .verdict

Where ``doc.paginate(...)`` (the bound op) walks a series inside a query plan, ``wc.paginate``
is the MANUAL/agent surface: a session an LLM driver can step page by page. Both share the advance
+ stop primitives (:mod:`..document.paginate`) and are driven by a :class:`~webclient.loop.BoundedLoop`.
State + config are the ``IPagination`` model (:mod:`.models`); behaviour is the
:class:`~.backing.PaginationBacking`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, AsyncIterator, ClassVar, Iterator, cast

from pydantic import PrivateAttr

from ..document.paginate import _auto_advance, _page_fingerprint
from ..session_core import SessionCore
from ..web_core import Backing
from .models import IPagination, PaginationConfig, PaginationVerdict  # noqa: F401  (re-exported)

from .backing import PaginationBacking

if TYPE_CHECKING:
    from ..client import WebClient
    from ..document import Document
    from ..reference import Reference


class Pagination(SessionCore, IPagination):
    """A stateful pagination walk. State (config / pages / verdict) is the ``IPagination`` model it
    inherits; this core adds the client binding and the advance state (the last page, the resolved
    ``by``/``name``, the seen-fingerprints ledger). A context manager; its ops (``step`` / ``run`` /
    ``done``) are the :class:`PaginationBacking`. Under a remote client the walk runs as ONE plan
    (``reference(url).resolve().paginate(...)``) server-side over ``/execute`` and the pages ride
    back; interactive ``step`` (a live, mutating walk) stays local, like ``Crawl.step``."""

    _client: "WebClient" = PrivateAttr(default=None)  # type: ignore[assignment]
    _store: dict[str, Any] = PrivateAttr(default_factory=dict)  # SessionCore.store (unused for now)
    _source: Any = PrivateAttr(default=None)  # the Reference to page one
    _current: Any = PrivateAttr(default=None)  # the last page fetched (the advance reads it)
    _by: str = PrivateAttr(default="auto")  # the resolved advance ("auto" until page one lands)
    _name: str = PrivateAttr(default="page")  # the resolved param name
    _seen: set[int] = PrivateAttr(default_factory=set)  # page fingerprints (clamp guard)
    _stop: str = PrivateAttr(default="")  # a post-fetch terminal cause (empty / clamp)
    _loop: Any = PrivateAttr(default=None)  # the BoundedLoop driving the walk (lazy, loop-bound)

    BACKINGS: ClassVar[tuple[Backing, ...]] = (PaginationBacking(),)

    def bind(self, client: "WebClient", source: Any) -> "Pagination":
        """Share ``client``'s engine and set page one. ``source`` is a URL / :class:`Reference`
        (page one is fetched on the first step) or an already-fetched :class:`Document` (used as
        page one directly -- no re-fetch). The advance ``by`` is resolved off page one when
        ``by="auto"``; for a URL source that happens after the first fetch."""
        from ..document import Document
        from ..reference import from_url

        self._client = client
        self._by, self._name = self.config.by, self.config.name
        if isinstance(source, Document):  # page one already in hand -- seed it
            self.pages = [source]
            self._current = source
            self._seen = {_page_fingerprint(source)}
            self._source = source._ref if source._ref is not None else from_url(source.final_url or source.url)
            if self._by == "auto":
                self._by, self._name = _auto_advance(source, self.config.name)
        else:  # a URL / Reference -- page one is fetched on the first round
            self._source = source if not isinstance(source, str) else from_url(source)
            self._source._client = client
        return self

    # -- remote: the walk is data-producing, run it as one plan -----------------
    def _remote_call(self, op: str, is_prop: bool) -> Any:
        """Remotely, a pagination walk is DATA-PRODUCING, not a stateful server object: ``run``
        executes ``reference(url).resolve().paginate(...)`` as one plan over ``/execute`` (the
        config is fully serializable) and the pages ride back. Interactive ``step`` has no
        stateless plan form (a live walk), so it stays local -- run/stream remotely instead."""
        if op == "run":
            def _run(*a: Any, **k: Any) -> "Pagination":
                self.pages = list(self._remote_expr().collect())
                self.status = "closed"
                self.verdict = PaginationVerdict(
                    done=True, reason="done", rounds=len(self.pages), result="remote",
                    pages=len(self.pages), rows=0, stop="remote",
                )
                return self
            return _run
        if op == "step":
            def _step(*a: Any, **k: Any) -> "Pagination":
                raise NotImplementedError(
                    "a remote pagination walk runs as one plan (its state lives server-side); "
                    "interactive step() runs only on a local client -- use run() or stream()"
                )
            return _step
        return super()._remote_call(op, is_prop)

    def _remote_expr(self) -> Any:
        """The ``reference(url).resolve().paginate(...)`` plan that reruns THIS walk server-side --
        its source URL + config as ``paginate`` kwargs. Rooted at a reference, so ``.collect()``
        returns the pages over ``/execute``."""
        from ...interface import wq

        c = self.config
        return (
            wq.reference(self._source.url).resolve().paginate(
                by=c.by, max_pages=c.max_pages, max_rows=c.max_rows, name=c.name,
                start=c.start, step=c.step, size=c.size, cursor=c.cursor,
                cursor_attr=c.cursor_attr, records=c.records, until=c.until,
                until_before=c.until_before,
            )
        )

    # -- streaming: the same engine as run(), consumed incrementally -----------
    def stream(self) -> "Iterator[Any]":
        """Stream the walk: yield each page as it is fetched (``for page in pg.stream()``). Breaking
        pauses the walk (state intact, so re-entering or calling ``run()`` continues). ``run()`` is
        this stream drained. (Not ``__iter__`` -- iterating a pydantic model yields its fields.)"""
        if self._dispatch_mode() == "remote":
            return self._remote_stream()
        backing = cast(PaginationBacking, self.BACKINGS[0])
        return self._client.loop().stream(backing._astream(self))

    def _remote_stream(self) -> "Iterator[Any]":
        """Remote streaming: run the walk as one plan (no per-round handshake) and yield its pages."""
        self.dispatch("run")
        yield from self.pages

    def astream(self) -> "AsyncIterator[Any]":
        """The async twin of :meth:`stream`: ``async for page in pg.astream()``."""
        if self._dispatch_mode() == "remote":
            async def _aiter() -> "AsyncIterator[Any]":
                self.dispatch("run")
                for page in self.pages:
                    yield page
            return _aiter()
        backing = cast(PaginationBacking, self.BACKINGS[0])
        return self._client.loop().astream(backing._astream(self))


__all__ = ["Pagination", "PaginationConfig", "PaginationVerdict", "IPagination"]
