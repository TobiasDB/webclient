"""DocumentCore: the core behind a document.

Core Fields = the resolved response (the surface's data). Backings = per-medium
op providers, one module each: :mod:`.html` (HtmlBacking -- css/xpath select,
attr, text_content, render), :mod:`.json` (JsonBacking -- dotted path),
:mod:`.status` (StatusBacking -- ok/error/is_ok/reload/summary), :mod:`.events`
(EventBacking) and :mod:`..live` (LiveBacking -- browser interaction). A selected
element is itself a DocumentCore (subtree / json sub-value), so selection nests.
Shared helpers (``Element``/``_element``/``_override``) live in :mod:`._shared`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar, Literal

from pydantic import BaseModel, PrivateAttr

from ...errors import WebError
from ..web_core import Backing, WebCore
from .live import LiveBacking
from ._shared import Element, _element, _override  # noqa: F401  (re-exported)
from .events import EventBacking
from .html import HtmlBacking
from .json import JsonBacking
from .status import StatusBacking
from .summary import (
    MetadataBacking,
    StructureBacking,
    SummaryBacking,
    TransportBacking,
)

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

    _client: Any = PrivateAttr(default=None)  # owning WebClientCore
    _ref: Any = PrivateAttr(default=None)  # the ReferenceCore that produced it
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

    BACKINGS: ClassVar[tuple[Backing, ...]] = (
        StatusBacking(),
        EventBacking(),
        LiveBacking(),
        HtmlBacking(),
        JsonBacking(),
        TransportBacking(),
        MetadataBacking(),
        StructureBacking(),
        SummaryBacking(),
    )

    @property
    def ok(self) -> bool:
        if self.error is not None or self._missing:
            return False
        return 200 <= self.status_code < 300 or self.status_code == 0


__all__ = ["DocumentCore", "Element", "HtmlBacking", "JsonBacking"]
