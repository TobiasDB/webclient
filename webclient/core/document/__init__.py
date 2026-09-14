"""DocumentCore: the core behind a document.

Core Fields = the resolved response (the surface's data). Backings = per-medium
op providers, one module each: :mod:`.html` (HtmlBacking -- css/xpath select,
attr, text_content, render), :mod:`.json` (JsonBacking -- dotted path),
:mod:`.status` (StatusBacking -- ok/error/is_ok/reload/summary), :mod:`.events`
(EventBacking) and :mod:`..live` (LiveBacking -- browser interaction). A selected
element is itself a DocumentCore (subtree / json sub-value), so selection nests:
a selection backing asks the core for the child via ``DocumentCore._sub`` (it owns
the sub-core wiring), and the ``Element`` value type lives in :mod:`...models`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar, Literal

from pydantic import BaseModel, PrivateAttr

from ...errors import WebError
from ...models import Element  # noqa: F401  (re-exported as the document's block type)
from ..web_core import Backing, WebCore
from .live import LiveBacking
from .events import EventBacking
from .html import HtmlBacking
from .json import JsonBacking
from .status import StatusBacking
from .summary import (
    MetadataBacking,
    RuntimeBacking,
    StructureBacking,
    SummaryBacking,
    TransportBacking,
)

if TYPE_CHECKING:
    from ..client import WebClientCore
    from ..reference import ReferenceCore

if TYPE_CHECKING:
    from ..client import WebClientCore  # noqa: F401


class DocumentCore(WebCore, BaseModel):
    """A resolved resource's core (+ element sub-cores). Core Fields are the
    response; behaviour is the backings."""

    id: str = ""
    name: str = ""  # scoped document name
    root: str = ""  # the originating reference's name
    session_id: str = ""  # owning session (if any)
    kind: Literal["html", "json", "xml", "binary"] = "html"
    url: str = ""
    final_url: str | None = None
    content: bytes = b""
    status_code: int = 0
    response_headers: dict[str, str] = {}
    encoding: str | None = None
    elapsed: float | None = None
    created: float = 0.0
    accessed: float = 0.0
    error: WebError | None = None

    # non-optional: a document is client-bound before any op (see ReferenceCore).
    _client: "WebClientCore" = PrivateAttr(default=None)  # type: ignore[assignment]
    _ref: "ReferenceCore | None" = PrivateAttr(default=None)  # producing reference
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
        RuntimeBacking(),
        SummaryBacking(),
    )

    @property
    def ok(self) -> bool:
        if self.error is not None or self._missing:
            return False
        return 200 <= self.status_code < 300 or self.status_code == 0

    def _sub(self, node: Any) -> "DocumentCore":
        """A selected element / sub-value as a child ``DocumentCore`` rooted at
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
        sub = DocumentCore(
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


__all__ = ["DocumentCore", "Element", "HtmlBacking", "JsonBacking"]
