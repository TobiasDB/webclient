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

from typing import TYPE_CHECKING, Any, ClassVar, Literal, TypeVar, overload

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
    # names the generated ``IDocument`` op annotations resolve against (its ops
    # return real cores/collections/value models).
    from ...collection import Collection, Field
    from ...models import (
        ActionEvent,
        ConsoleEvent,
        DOMUpdateEvent,
        Event,
        Metadata,
        Runtime,
        Structure,
        Summary,
        Transport,
    )
    from ...surfaces.lazy import LazyDocument
    from ..client import WebClientCore  # noqa: F401
    from ..reference import ReferenceCore

    E = TypeVar("E", bound="Event")  # events_of(type[E]) -> list[E]

    class IDocument(BaseModel):
        """The eager ops ``DocumentCore`` implements, typed. Generated from the
        document backings; ``DocumentCore`` inherits it, so its ops are statically
        visible on the core itself (a backing / the core reaches them with no
        ``dispatch("...")`` string). Exists only for the type checker -- at runtime
        it is empty, so ``WebCore.__getattr__`` still dispatches every op."""

        # >>> generated: Document interface <<<
        # fmt: off
        @property
        def action_events(self) -> list[ActionEvent]: ...
        @property
        def console(self) -> list[ConsoleEvent]: ...
        @property
        def dom_mutations(self) -> list[DOMUpdateEvent]: ...
        @property
        def events(self) -> list[Event]: ...
        @property
        def message(self) -> str: ...
        @property
        def text_content(self) -> str: ...
        @property
        def title(self) -> str: ...
        @overload
        def attr(self, name: Literal['href', 'src', 'action']) -> "ReferenceCore": ...
        @overload
        def attr(self, name: str, *, error: Any = ...) -> "Field[str]": ...
        def click(self, selector: str | None = ..., *, timeout: float | None = ..., optional: bool = ...) -> "DocumentCore": ...
        def evaluate(self, script: str) -> "Any": ...
        @overload
        def events_of(self, event_type: type[E]) -> "list[E]": ...
        @overload
        def events_of(self, event_type: str) -> "list[Event]": ...
        def is_empty(self) -> "Field[bool]": ...
        def is_ok(self) -> "Field[bool]": ...
        def metadata(self) -> "Metadata": ...
        def ref(self) -> "ReferenceCore": ...
        def reload(self) -> "DocumentCore": ...
        @overload
        def render(self, format: Literal['elements']) -> "list[Element]": ...
        @overload
        def render(self, format: Literal['links']) -> "Collection[ReferenceCore]": ...
        @overload
        def render(self, format: str, **options: Any) -> "str": ...
        def runtime(self) -> "Runtime": ...
        def screenshot(self, selector: str | None = ...) -> "DocumentCore": ...
        def select(self, selector: str, *, index: int = ..., error: Any = ...) -> "DocumentCore": ...
        def select_all(self, selector: str, *, limit: int | None = ..., offset: int = ...) -> "Collection[DocumentCore]": ...
        def structure(self) -> "Structure": ...
        def summary(self, *include: str, exclude: Any = ...) -> "Summary": ...
        def transport(self) -> "Transport": ...
        def wait_for(self, selector: str | None = ..., *, timeout: float | None = ...) -> "DocumentCore": ...
        def write(self, selector: str, text: str, *, timeout: float | None = ..., optional: bool = ...) -> "DocumentCore": ...
        # fmt: on
        # >>> end generated <<<

else:

    class IDocument(BaseModel):  # runtime: empty -> never shadows __getattr__
        pass


class DocumentCore(WebCore, IDocument):
    """A resolved resource's core (+ element sub-cores). Core Fields are the
    response; behaviour is the backings. Its eager ops come from the generated
    ``IDocument`` interface it inherits; sync/async/remote are dispatch modes."""

    if TYPE_CHECKING:  # narrow WebCore.lazy (Any) to this core's lazy surface

        @property
        def lazy(self) -> "LazyDocument": ...

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
