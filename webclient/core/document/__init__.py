"""Document: the core behind a document.

Core Fields = the resolved response (the surface's data). Backings = per-medium
op providers, one module each: :mod:`.html` (HtmlBacking -- css/xpath select,
attr (incl. attr("text")), render), :mod:`.json` (JsonBacking -- dotted path),
:mod:`.status` (StatusBacking -- ok/error/is_ok/reload), the facet backings
(:mod:`.transport`/:mod:`.metadata`/:mod:`.structure`/:mod:`.signals`),
:mod:`.events` (EventBacking) and :mod:`..live` (LiveBacking). A selected
element is itself a Document (subtree / json sub-value), so selection nests:
a selection backing asks the core for the child via ``Document._sub`` (it owns
the sub-core wiring), and the ``Element`` value type lives in :mod:`...models`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar, TypeVar, overload

from pydantic import PrivateAttr

from ..web_core import Backing, WebCore
from .live import LiveBacking
from .models import Element, IDocument  # noqa: F401  (Element re-exported)
from .events import EventBacking
from .html import HtmlBacking
from .json import JsonBacking
from .status import StatusBacking
from .transport import TransportBacking
from .metadata import MetadataBacking
from .structure import StructureBacking
from .flags import FlagsBacking
from .regex import RegexBacking
from .element_index import ElementIndexBacking
from .paginate import PaginateBacking
from .patterns import PatternsBacking

if TYPE_CHECKING:
    from ...interface import LazyDocument
    from ...query.collection import Collection
    from ..client import WebClient  # noqa: F401
    from ..reference import Reference

M = TypeVar("M")  # a row model (a pydantic BaseModel) for project(model)


class Document(WebCore, IDocument):
    """A resolved resource's core (+ element sub-cores). Its Core Fields + eager
    ops come from the ``IDocument`` model/interface it inherits (:mod:`.models`);
    this core adds the behaviour -- the backings, dispatch, and ``_sub`` (the
    element sub-core factory). Sync/async/remote are dispatch modes."""

    if TYPE_CHECKING:  # narrow WebCore.lazy (Any) to this core's lazy surface

        @property
        def lazy(self) -> "LazyDocument":
            """This document's lazy surface -- ops build an ``Expr`` to run later, not now."""
            ...

    # non-optional: a document is client-bound before any op (see Reference).
    _client: "WebClient" = PrivateAttr(default=None)  # type: ignore[assignment]
    _ref: "Reference | None" = PrivateAttr(default=None)  # producing reference
    _element: Any = PrivateAttr(default=None)  # lxml element / json sub-value
    _tree: Any = PrivateAttr(default=None)  # cached lxml parse
    _data: Any = PrivateAttr(default=None)  # cached json
    _missing: bool = PrivateAttr(default=False)  # a selection that missed
    _events: list[Any] = PrivateAttr(default_factory=list)  # events routed here
    _errors: list[Any] = PrivateAttr(default_factory=list)  # errors that occurred on this doc's ops
    _pending: Any = PrivateAttr(default=None)  # an Ask a resolve driver raised (manual mode)
    _page: Any = PrivateAttr(default=None)  # playwright Page (live document)
    _lease: Any = PrivateAttr(default=None)  # the page's pool lease (live document)
    _keep_alive: bool = PrivateAttr(default=False)  # caller owns the page's lifecycle
    #                                                 (a plan won't auto-release it)
    _render_stats: dict[str, Any] = PrivateAttr(  # {text, nodes} of the settled render
        default_factory=dict
    )
    _stamps: list[dict[str, Any]] = PrivateAttr(  # append-only phase stamps ({node, xhr, t, text})
        default_factory=list  # the XHR->DOM correlation substrate (see correlate.py)
    )
    _xhr_bodies: dict[str, list[str]] = PrivateAttr(  # url -> XHR/fetch response texts (in order),
        default_factory=dict  # for the content-matching ContentCorrelator (populated by the browser)
    )
    _flag_cache: Any = PrivateAttr(default=None)  # memoised flag set (one detection pass/doc)
    _row: Any = PrivateAttr(default=None)  # extracted columns (extract/field)
    _surface: Any = PrivateAttr(default=None)  # the core's single eager surface
    _set_cookies: dict[str, str] = PrivateAttr(  # transport-parsed Set-Cookie
        default_factory=dict
    )
    #: the pre-JS (static) HTML for a browser-rendered document (auto escalation),
    #: so ``skeleton()`` can mark nodes server-initial vs client-injected.
    _static_html: "bytes | None" = PrivateAttr(default=None)
    #: the transport tiers this resolution took, e.g. ``["static"]`` or
    #: ``["static", "proxy", "browser"]`` -- read by the ``transport`` facet.
    _tiers: list[str] = PrivateAttr(default_factory=list)
    #: a server-side handle (remote dispatcher): it holds no local content, so its
    #: content ops round-trip. Set by ``core.service.deserialize`` on the wire.
    _remote_handle: bool = PrivateAttr(default=False)
    #: the plan (an ``Expr``) that produced THIS remote handle, stamped by the engine's
    #: ``core.service.ServiceTransport.execute``. If the server evicts the handle (a retriable
    #: ``NoSuchDocument``), a content op re-runs this to reproduce a fresh handle and
    #: retries once -- stateless-by-choice makes the replay safe (see ``_remote_call``).
    _remote_source: Any = PrivateAttr(default=None)

    BACKINGS: ClassVar[tuple[Backing, ...]] = (
        StatusBacking(),
        EventBacking(),
        LiveBacking(),
        HtmlBacking(),
        JsonBacking(),
        TransportBacking(),
        MetadataBacking(),
        StructureBacking(),
        FlagsBacking(),
        RegexBacking(),
        ElementIndexBacking(),
        PaginateBacking(),
        PatternsBacking(),
    )

    @property
    def pending(self) -> Any:
        """The :class:`~webclient.loop.Ask` a resolve driver raised on this hop (``None`` when
        nothing is waiting): answer it by hand with ``wc.escalate(doc, tier)``."""
        return self._pending

    @property
    def ok(self) -> bool:
        """Whether the document resolved cleanly -- no error, not a miss, and a 2xx (or the
        in-memory ``0`` status of a locally built document)."""
        if self.error is not None or self._missing:
            return False
        return 200 <= self.status_code < 300 or self.status_code == 0

    def _sub(self, node: Any) -> "Document":
        """A selected element / sub-value as a child ``Document`` rooted at
        this one -- a ``None`` node means the selection missed (a not-ok, empty
        sub-document). The core owns this construction so a selection backing
        (html / json) never hand-wires a sub-core's internals (client, root, the
        shared event store, the missing flag): it just hands over the node.

        The element's ``content`` bytes are NOT serialised here -- every op reads the
        live ``_element`` directly (``select``/``attr``/``text_content`` via ``_tree``),
        and ``html()`` serialises on demand. Eagerly ``tostring``-ing each selected node
        was the crawl/query hot path's dominant allocation (profiled), for bytes almost
        nothing consumes."""
        sub = Document(
            url=self.url,
            final_url=self.final_url,
            kind=self.kind,
            status_code=self.status_code,
        )
        sub.root = self.name or self.root
        sub._client = self._client
        sub._element = node
        sub._missing = node is None
        sub._events = self._events  # a static element shares the store
        return sub

    # -- row shaping: a document is a single element (a "collection of one") -----
    # extract evaluates several named expressions against THIS document and stages
    # them as its row; project renders that row. These mirror the Collection ops
    # (which are just this primitive fanned out) so a lone Document is usable the
    # same way -- ``doc.extract(spa=doc.spa()).project()``. Hand-written (like
    # Collection/Field), not backings, so they are not lifted or fanned out.
    async def aextract(self, **exprs: Any) -> "Document":
        """Evaluate each named expression against this document and stage the
        results as its ``_row`` (in order, so a later column can read an earlier
        one via ``field``; chained extracts accumulate). Loud by default -- a
        column whose select/attr misses raises; mark it ``error=RETURN`` for a
        ``None``. THE single-element extraction (``Collection.aextract`` fans it
        out); returns the document so extracts chain."""
        from ...query.collection import apply_extract

        await apply_extract(self, exprs, self._client)
        return self

    def extract(self, **exprs: Any) -> "Document":
        """Eager form of :meth:`aextract` (bridged onto the engine loop)."""
        return self._client.loop().run(self.aextract(**exprs))

    @overload
    def project(self) -> dict[str, Any]:
        """Project the extracted row to a plain ``dict``."""
        ...
    @overload
    def project(self, model: type[M]) -> M:
        """Project the extracted row validated into ``model``."""
        ...
    def project(self, model: "type[M] | None" = None) -> "dict[str, Any] | M":
        """This document's extracted row as plain data: a ``Reference`` column
        (e.g. from ``attr('href')``) becomes its URL string and a ``Field`` its
        value, so the row is JSON-ready. One ``dict`` (not a list) -- a document
        is one row. Pass ``model`` to validate the row into it (eager only)."""
        from ...query.collection import _project_row, _row_of

        data = _project_row(_row_of(self, create=False) or {})
        if model is None:
            return data
        validate = getattr(model, "model_validate", None)
        return validate(data) if validate is not None else model(**data)

    # -- pagination: walk this dataset's pages into a Collection --------------------
    # A BOUND op (hand-written, like extract), so the executor hands ``stop``/``key``
    # to it UNEVALUATED and the walk evaluates them per page -- a semantic stop the
    # DSL expresses, not just a literal cutoff. The walk itself lives in ``.paginate``
    # (the module); this is the thin method that runs it and lifts the pages to a
    # Collection so ``select_all(...).extract(...).project()`` fans out across them.
    async def apaginate(
        self,
        *,
        by: str = "auto",
        max_pages: int = 20,
        max_rows: int = 0,
        name: str = "page",
        start: int = 1,
        step: int = 1,
        size: int = 0,
        cursor: str = "",
        cursor_attr: str = "text",
        records: str = "",
        until: str = "",
        until_before: str = "",
        total_pages: int = 0,
        action: Any = None,
        partition_param: str = "",
        partition_values: "list[str] | tuple[str, ...]" = (),
        stop: Any = None,
        key: Any = None,
    ) -> "Collection[Document]":
        """The pages of this dataset as a ``Collection[Document]``, page one first -- chain
        ``select_all(...).extract(...).project()`` to extract the WHOLE dataset (the body runs
        across every page, not page one only).

        HOW TO ADVANCE (``by``): ``"auto"`` (default) picks the advance from the page's detected
        ``pagination`` hint -- a ``?page=``/``?offset=`` source walks by that param, everything else
        follows the next link -- so a bare ``doc.paginate()`` just works. ``"link"`` follows ``rel=next``
        (an HTML ``a/link[rel=next]`` or an HTTP ``Link:`` header, so an API paginates); ``"param"``
        walks ``?{name}=`` from ``start`` by ``step`` (or by ``size`` as an offset); ``"cursor"`` reads
        a keyset token off each page (the ``cursor`` selector's ``cursor_attr`` -- ``cursor="a.next"`` +
        ``cursor_attr="data-after"``, or a JSON path ``cursor="pageInfo.endCursor"``) and carries it in
        ``?{name}=``.

        WHERE TO STOP (all optional, so a long dataset isn't walked whole for a few rows): ``max_pages``
        caps the page count; ``max_rows`` with ``records`` (the record selector) stops once that many
        rows are collected; ``until`` (a per-record ordering field) with ``until_before`` stops after
        the first page whose OLDEST value sorts below the cutoff (the recency case); ``stop`` is a
        predicate expression evaluated against each page (``stop=wq.doc.select('.last').is_ok()`` --
        truthy means this page is the last). ``key`` is an expression giving each page a dedup key
        (``key=wq.doc.select('article', index=0).attr('text')``); a repeated key stops the walk (a
        semantic clamp guard, for pages that repeat records but differ in chrome/timestamps)."""
        from ...query.collection import Collection
        from .paginate import walk

        pages = await walk(
            self, by=by, max_pages=max_pages, max_rows=max_rows, name=name, start=start,
            step=step, size=size, cursor=cursor, cursor_attr=cursor_attr, records=records,
            until=until, until_before=until_before, total_pages=total_pages, action=action,
            partition_param=partition_param, partition_values=partition_values,
            stop=stop, key=key, client=self._client,
        )
        return Collection(pages, client=self._client, root=self.name or self.root)

    def paginate(
        self,
        *,
        by: str = "auto",
        max_pages: int = 20,
        max_rows: int = 0,
        name: str = "page",
        start: int = 1,
        step: int = 1,
        size: int = 0,
        cursor: str = "",
        cursor_attr: str = "text",
        records: str = "",
        until: str = "",
        until_before: str = "",
        total_pages: int = 0,
        action: Any = None,
        partition_param: str = "",
        partition_values: "list[str] | tuple[str, ...]" = (),
        stop: Any = None,
        key: Any = None,
    ) -> "Collection[Document]":
        """Eager form of :meth:`apaginate` (bridged onto the engine loop)."""
        return self._client.loop().run(self.apaginate(
            by=by, max_pages=max_pages, max_rows=max_rows, name=name, start=start, step=step,
            size=size, cursor=cursor, cursor_attr=cursor_attr, records=records, until=until,
            until_before=until_before, total_pages=total_pages, action=action,
            partition_param=partition_param, partition_values=partition_values, stop=stop, key=key,
        ))


__all__ = ["Document", "Element", "HtmlBacking", "JsonBacking"]
