"""Document: the core behind a document.

Core Fields = the resolved response (the surface's data). Backings = per-medium
op providers, one module each: :mod:`.html` (HtmlBacking -- css/xpath select,
attr, text_content, render), :mod:`.json` (JsonBacking -- dotted path),
:mod:`.status` (StatusBacking -- ok/error/is_ok/reload/summary), :mod:`.events`
(EventBacking) and :mod:`..live` (LiveBacking -- browser interaction). A selected
element is itself a Document (subtree / json sub-value), so selection nests:
a selection backing asks the core for the child via ``Document._sub`` (it owns
the sub-core wiring), and the ``Element`` value type lives in :mod:`...models`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

from pydantic import PrivateAttr

from ..web_core import Backing, WebCore
from .live import LiveBacking
from .models import Element, IDocument  # noqa: F401  (Element re-exported)
from .events import EventBacking
from .html import HtmlBacking
from .json import JsonBacking
from .status import StatusBacking
from .summary import (
    MetadataBacking,
    ProbeBacking,
    RuntimeBacking,
    StructureBacking,
    SummaryBacking,
    TransportBacking,
)

if TYPE_CHECKING:
    from ...surfaces.lazy import LazyDocument
    from ..client import WebClient  # noqa: F401
    from ..reference import Reference


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
    _row: Any = PrivateAttr(default=None)  # extracted columns (extract/field)
    _surface: Any = PrivateAttr(default=None)  # the core's single eager surface
    _set_cookies: dict[str, str] = PrivateAttr(  # transport-parsed Set-Cookie
        default_factory=dict
    )
    #: the pre-JS (static) HTML for a browser-rendered document (probe / auto
    #: escalation), so ``skeleton()`` can mark nodes server-initial vs client-injected.
    _static_html: "bytes | None" = PrivateAttr(default=None)
    #: a server-side handle (remote dispatcher): it holds no local content, so its
    #: content ops round-trip. Set by ``RemoteWebClientCore`` on deserialize.
    _remote_handle: bool = PrivateAttr(default=False)
    #: what the transport ladder had to escalate to (browser/proxy/anti-bot), or
    #: None on a plain static fetch. Read by the ``probe`` summary facet.
    _probe: Any = PrivateAttr(default=None)  # ProbeRecord | None

    BACKINGS: ClassVar[tuple[Backing, ...]] = (
        StatusBacking(),
        EventBacking(),
        LiveBacking(),
        HtmlBacking(),
        JsonBacking(),
        TransportBacking(),
        MetadataBacking(),
        StructureBacking(),
        RuntimeBacking(),
        ProbeBacking(),
        SummaryBacking(),
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


__all__ = ["Document", "Element", "HtmlBacking", "JsonBacking"]
