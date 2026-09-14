"""Concrete eager surfaces. The class scaffolding is hand-written; the op
signature blocks (marked ``>>> generated <<<``) are produced by
``scripts.gen_stubs`` from each core's fields + its backings' typed ops."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal, TypeVar, cast, overload

from ..core.client import WebClientCore
from ..core.client import async_client as _async_client
from ..core.document import DocumentCore
from ..core.reference import HttpMethod, ReferenceCore
from ..core.reference import from_url as _core_from_url
from ..core.session import WebSessionCore

if TYPE_CHECKING:
    from ..collection import Collection, Field
    from ..core.document import Element
    from ..events import ActionEvent, ConsoleEvent, DOMUpdateEvent, Event, EventBus
    from ..pool import ClientPool
    from ..summary import Metadata, Runtime, Structure, Summary, Transport
    from .lazy import LazyDocument, LazyReference, LazyWebClient

T = TypeVar("T")
E = TypeVar("E", bound="Event")  # an event subtype, for events_of(cls) -> list[cls]


if TYPE_CHECKING:  # the eager surfaces are pure typing stubs over their cores

    class Reference(ReferenceCore):
        """A request spec (eager) -- the ``ReferenceCore`` itself, typed with its
        ops (``url``/``with_params``/``replace``/``join``/``resolve``). A pure
        typing stub: at runtime ``Reference is ReferenceCore`` and the core
        dispatches its own ops (``WebCore.__getattr__``)."""

        @property
        def lazy(self) -> "LazyReference": ...  # a recorder bound to this ref

        # >>> generated: Reference eager surface <<<
        # fmt: off
        @property
        def url(self) -> str: ...
        def join(self, href: str) -> "Reference": ...
        def replace(self, **fields: Any) -> "Reference": ...
        def resolve(self, *, browser: bool = ..., optional: bool = ..., error: Any = ...) -> "Document": ...
        def with_params(self, **params: str) -> "Reference": ...
        # fmt: on
        # >>> end generated <<<

    class Document(DocumentCore):
        """A resolved document (eager) -- the ``DocumentCore`` itself, typed with
        its ops (``select``/``attr``/``text_content``/``render``/events + the live
        interaction set). A pure typing stub: at runtime ``Document is
        DocumentCore``."""

        @property
        def lazy(self) -> "LazyDocument": ...  # a recorder bound to this document

        # >>> generated: Document eager surface <<<
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
        def attr(self, name: Literal['href', 'src', 'action']) -> "Reference": ...  # type: ignore[overload-overlap]
        @overload
        def attr(self, name: str, *, error: Any = ...) -> "Field[str]": ...
        def click(self, selector: str | None = ..., *, timeout: float | None = ..., optional: bool = ...) -> "Document": ...
        def evaluate(self, script: str) -> "Any": ...
        @overload
        def events_of(self, event_type: type[E]) -> "list[E]": ...
        @overload
        def events_of(self, event_type: str) -> "list[Event]": ...
        def is_empty(self) -> "Field[bool]": ...
        def is_ok(self) -> "Field[bool]": ...
        def metadata(self) -> "Metadata": ...
        def ref(self) -> "Reference": ...
        def reload(self) -> "Document": ...
        @overload
        def render(self, format: Literal['elements']) -> "list[Element]": ...
        @overload
        def render(self, format: Literal['links']) -> "Collection[Reference]": ...
        @overload
        def render(self, format: str, **options: Any) -> "str": ...
        def runtime(self) -> "Runtime": ...
        def screenshot(self, selector: str | None = ...) -> "Document": ...
        def select(self, selector: str, *, index: int = ..., error: Any = ...) -> "Document": ...
        def select_all(self, selector: str, *, limit: int | None = ..., offset: int = ...) -> "Collection[Document]": ...
        def structure(self) -> "Structure": ...
        def summary(self, *include: str, exclude: Any = ...) -> "Summary": ...
        def transport(self) -> "Transport": ...
        def wait_for(self, selector: str | None = ..., *, timeout: float | None = ...) -> "Document": ...
        def write(self, selector: str, text: str, *, timeout: float | None = ..., optional: bool = ...) -> "Document": ...
        # fmt: on
        # >>> end generated <<<

    class WebClient(WebClientCore):
        """The synchronous eager client -- a ``WebClientCore`` itself, typed with
        its authoring verbs. Eager: ``wc.fetch(url)`` resolves and returns a
        ``Document`` (no ``.collect()``); ``wc.ref(url)`` a ``Reference``. Batch or
        defer with ``wc.lazy`` (records a plan). A pure typing stub: at runtime
        ``WebClient is WebClientCore``; the ``async``/remote clients are the same
        surface over a different-dispatcher core."""

        @property
        def lazy(self) -> "LazyWebClient": ...  # record a plan to batch/defer
        @property
        def bus(self) -> "EventBus": ...  # subscribe to network/dom/console topics
        @property
        def pool(self) -> "ClientPool": ...  # transport-lease pool (``.stats()``)
        def session(  # a scoped identity sharing this client's engine
            self, *, ttl: float | None = ..., headers: dict[str, str] | None = ..., **kw: Any
        ) -> "Session": ...

        # >>> generated: WebClient eager surface <<<
        # fmt: off
        def fetch(self, url: Any, *, optional: bool = ..., error: Any = ..., **kw: Any) -> "Document": ...
        def ref(self, url: Any, method: str = ..., **kw: Any) -> "Reference": ...
        def summary(self, url: Any, *include: str, **kw: Any) -> "Summary": ...
        # fmt: on
        # >>> end generated <<<

    class Session(WebSessionCore):
        """A session (eager) -- a scoped ``WebClientCore`` with its own identity
        (cookies/headers/ttl). ``session.fetch(url)`` / ``session.ref(url)`` resolve
        eagerly, threading the session identity. A pure typing stub: at runtime
        ``Session is WebSessionCore``."""

        @property
        def lazy(self) -> "LazyWebClient": ...

        # >>> generated: Session eager surface <<<
        # fmt: off
        def fetch(self, url: Any, *, optional: bool = ..., error: Any = ..., **kw: Any) -> "Document": ...
        def ref(self, url: Any, method: str = ..., **kw: Any) -> "Reference": ...
        def summary(self, url: Any, *include: str, **kw: Any) -> "Summary": ...
        # fmt: on
        # >>> end generated <<<

    class AsyncReference(ReferenceCore):
        """The async view of a :class:`Reference`: ``resolve`` is awaitable
        (``await ref.resolve()``); every other op is the same in-memory surface,
        and Core-returning ops stay in the async tier. A pure typing stub: at
        runtime ``AsyncReference is ReferenceCore`` -- the async-ness comes from the
        bound client's dispatcher (``_mode``), not the type."""

        @property
        def lazy(self) -> "LazyReference": ...

        # >>> generated: AsyncReference surface <<<
        # fmt: off
        url: "str"
        def join(self, href: str) -> "AsyncReference": ...
        def replace(self, **fields: Any) -> "AsyncReference": ...
        async def resolve(self, *, browser: bool = ..., optional: bool = ..., error: Any = ...) -> "AsyncDocument": ...
        def with_params(self, **params: str) -> "AsyncReference": ...
        # fmt: on
        # >>> end generated <<<

    class AsyncDocument(DocumentCore):
        """The async view of a :class:`Document`: in-memory ops are synchronous;
        Core-returning ops stay in the async tier so a later IO op is awaitable
        (``await doc.select('a').attr('href').resolve()``). A pure typing stub: at
        runtime ``AsyncDocument is DocumentCore``."""

        @property
        def lazy(self) -> "LazyDocument": ...

        # >>> generated: AsyncDocument surface <<<
        # fmt: off
        action_events: "list[ActionEvent]"
        console: "list[ConsoleEvent]"
        dom_mutations: "list[DOMUpdateEvent]"
        events: "list[Event]"
        message: "str"
        text_content: "str"
        title: "str"
        @overload
        def attr(self, name: Literal['href', 'src', 'action']) -> "AsyncReference": ...  # type: ignore[overload-overlap]
        @overload
        def attr(self, name: str, *, error: Any = ...) -> "Field[str]": ...
        async def click(self, selector: str | None = ..., *, timeout: float | None = ..., optional: bool = ...) -> "AsyncDocument": ...
        async def evaluate(self, script: str) -> "Any": ...
        @overload
        def events_of(self, event_type: type[E]) -> "list[E]": ...
        @overload
        def events_of(self, event_type: str) -> "list[Event]": ...
        def is_empty(self) -> "Field[bool]": ...
        def is_ok(self) -> "Field[bool]": ...
        def metadata(self) -> "Metadata": ...
        def ref(self) -> "AsyncReference": ...
        async def reload(self) -> "AsyncDocument": ...
        @overload
        def render(self, format: Literal['elements']) -> "list[Element]": ...
        @overload
        def render(self, format: Literal['links']) -> "Collection[AsyncReference]": ...
        @overload
        def render(self, format: str, **options: Any) -> "str": ...
        def runtime(self) -> "Runtime": ...
        async def screenshot(self, selector: str | None = ...) -> "AsyncDocument": ...
        def select(self, selector: str, *, index: int = ..., error: Any = ...) -> "AsyncDocument": ...
        def select_all(self, selector: str, *, limit: int | None = ..., offset: int = ...) -> "Collection[AsyncDocument]": ...
        def structure(self) -> "Structure": ...
        def summary(self, *include: str, exclude: Any = ...) -> "Summary": ...
        def transport(self) -> "Transport": ...
        async def wait_for(self, selector: str | None = ..., *, timeout: float | None = ...) -> "AsyncDocument": ...
        async def write(self, selector: str, text: str, *, timeout: float | None = ..., optional: bool = ...) -> "AsyncDocument": ...
        # fmt: on
        # >>> end generated <<<

else:  # at runtime a surface IS its core
    Reference = ReferenceCore
    Document = DocumentCore
    WebClient = WebClientCore
    Session = WebSessionCore
    AsyncReference = ReferenceCore
    AsyncDocument = DocumentCore

#: A live (browser-backed) document is a Document with the ``page`` capability.
LiveDocument = Document


def from_url(
    url: str,
    method: HttpMethod = "get",
    params: dict[str, str | list[str]] | None = None,
    headers: dict[str, str] | None = None,
    cookies: dict[str, str] | None = None,
) -> Reference:
    """Build a :class:`Reference` from a URL string."""
    return cast("Reference", _core_from_url(url, method, params, headers, cookies))


_DEFAULT: "WebClient | None" = None


def default_client() -> "WebClient":
    """A process-local shared client, recreated after it is closed."""
    global _DEFAULT
    if _DEFAULT is None or _DEFAULT._closed:
        _DEFAULT = cast("WebClient", WebClientCore())
    return _DEFAULT


class Renderer:
    """A (kind, format) render override -- register with ``wc.use``. Subclass
    and set ``name``/``kind``/``formats`` and implement ``render``."""

    name: str = ""
    kind: str = "html"
    formats: list[str] = []

    def render(self, document: "Document", format: str, **options: Any) -> Any:
        raise NotImplementedError


if TYPE_CHECKING:

    class AsyncWebClient(WebClientCore):
        """The async eager client -- the very same core with async dispatch (an
        instance flag, not a subclass): ``doc = await ac.fetch(url)`` and
        ``await ac.ref(url).resolve()`` chain async through the ``Async*`` surface
        types; in-memory ops on a resolved document are synchronous. At runtime a
        factory (``core.client.async_client``) setting mode "async", so its IO
        ops hand back an awaitable via ``bridge``."""

        @property
        def lazy(self) -> "LazyWebClient": ...
        @property
        def bus(self) -> "EventBus": ...
        @property
        def pool(self) -> "ClientPool": ...
        def session(
            self, *, ttl: float | None = ..., headers: dict[str, str] | None = ..., **kw: Any
        ) -> "Session": ...

        # >>> generated: AsyncWebClient surface <<<
        # fmt: off
        async def fetch(self, url: Any, *, optional: bool = ..., error: Any = ..., **kw: Any) -> "AsyncDocument": ...
        def ref(self, url: Any, method: str = ..., **kw: Any) -> "AsyncReference": ...
        async def summary(self, url: Any, *include: str, **kw: Any) -> "Summary": ...
        # fmt: on
        # >>> end generated <<<

else:  # at runtime the async client is the core in async-dispatcher mode
    AsyncWebClient = _async_client


def RemoteWebClient(url: str, token: str | None = None) -> "WebClient":
    """A ``WebClient`` over a remote core -- literally the same eager surface, run
    server-side. A factory, not a subclass: the remote-ness is entirely in the
    core (``RemoteWebClientCore``), a different-dispatcher ``WebClientCore`` whose
    client verbs round-trip a one-step plan to the service."""
    from ..core.remote import RemoteWebClientCore

    return cast("WebClient", RemoteWebClientCore(url=url, token=token))


__all__ = [
    "Reference",
    "Document",
    "AsyncReference",
    "AsyncDocument",
    "LiveDocument",
    "Session",
    "WebClient",
    "AsyncWebClient",
    "RemoteWebClient",
    "default_client",
    "from_url",
]
