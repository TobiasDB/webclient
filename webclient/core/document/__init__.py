"""Document: the core behind a document.

Core Fields = the resolved response (the surface's data). Backings = per-medium
op providers, one module each: :mod:`.html` (HtmlBacking -- css/xpath select,
attr, text_content, render), :mod:`.json` (JsonBacking -- dotted path),
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
from .signals import SignalsBacking

if TYPE_CHECKING:
    from ...surfaces.lazy import LazyDocument
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
        def lazy(self) -> "LazyDocument": ...

    # non-optional: a document is client-bound before any op (see Reference).
    _client: "WebClient" = PrivateAttr(default=None)  # type: ignore[assignment]
    _ref: "Reference | None" = PrivateAttr(default=None)  # producing reference
    _element: Any = PrivateAttr(default=None)  # lxml element / json sub-value
    _tree: Any = PrivateAttr(default=None)  # cached lxml parse
    _data: Any = PrivateAttr(default=None)  # cached json
    _missing: bool = PrivateAttr(default=False)  # a selection that missed
    _events: list[Any] = PrivateAttr(default_factory=list)  # events routed here
    _page: Any = PrivateAttr(default=None)  # playwright Page (live document)
    _lease: Any = PrivateAttr(default=None)  # the page's pool lease (live document)
    _keep_alive: bool = PrivateAttr(default=False)  # caller owns the page's lifecycle
    #                                                 (a plan won't auto-release it)
    _render_stats: dict[str, Any] = PrivateAttr(  # {text, nodes} of the settled render
        default_factory=dict
    )
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
    #: content ops round-trip. Set by ``RemoteWebClientCore`` on deserialize.
    _remote_handle: bool = PrivateAttr(default=False)

    BACKINGS: ClassVar[tuple[Backing, ...]] = (
        StatusBacking(),
        EventBacking(),
        LiveBacking(),
        HtmlBacking(),
        JsonBacking(),
        TransportBacking(),
        MetadataBacking(),
        StructureBacking(),
        SignalsBacking(),
    )

    @property
    def ok(self) -> bool:
        if self.error is not None or self._missing:
            return False
        return 200 <= self.status_code < 300 or self.status_code == 0

    def _sub(self, node: Any) -> "Document":
        """A selected element / sub-value as a child ``Document`` rooted at
        this one -- a ``None`` node means the selection missed (a not-ok, empty
        sub-document). The core owns this construction so a selection backing
        (html / json) never hand-wires a sub-core's internals (client, root, the
        shared event store, the missing flag): it just hands over the node."""
        content = b""
        if node is not None and not isinstance(node, (str, int, float, bool, list, dict)):
            try:
                from lxml import html as _lh

                content = _lh.tostring(node)  # the element's own bytes
            except Exception:
                content = b""
        sub = Document(
            url=self.url,
            final_url=self.final_url,
            kind=self.kind,
            status_code=self.status_code,
            content=content,
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
        from ...collection import apply_extract

        await apply_extract(self, exprs, self._client)
        return self

    def extract(self, **exprs: Any) -> "Document":
        """Eager form of :meth:`aextract` (bridged onto the engine loop)."""
        return self._client.loop().run(self.aextract(**exprs))

    @overload
    def project(self) -> dict[str, Any]: ...
    @overload
    def project(self, model: type[M]) -> M: ...
    def project(self, model: "type[M] | None" = None) -> "dict[str, Any] | M":
        """This document's extracted row as plain data: a ``Reference`` column
        (e.g. from ``attr('href')``) becomes its URL string and a ``Field`` its
        value, so the row is JSON-ready. One ``dict`` (not a list) -- a document
        is one row. Pass ``model`` to validate the row into it (eager only)."""
        from ...collection import _project_row, _row_of

        data = _project_row(_row_of(self, create=False) or {})
        if model is None:
            return data
        validate = getattr(model, "model_validate", None)
        return validate(data) if validate is not None else model(**data)


__all__ = ["Document", "Element", "HtmlBacking", "JsonBacking"]
