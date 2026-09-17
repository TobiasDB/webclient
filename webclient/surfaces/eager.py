"""Concrete eager surfaces. The class scaffolding is hand-written; the op
signature blocks (marked ``>>> generated <<<``) are produced by
``scripts.gen_stubs`` from each core's fields + its backings' typed ops."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal, TypeVar, cast, overload

from ..core.client import WebClient
from ..core.client import async_client as _async_client
from ..core.document import Document
from ..core.reference import HttpMethod, Reference
from ..core.reference import from_url as _core_from_url
from ..core.session import Session

if TYPE_CHECKING:
    from ..collection import Collection, Field
    from ..core.document import Element
    from ..events import EventBus
    from ..models import ActionEvent, ConsoleEvent, DOMUpdateEvent, Event
    from ..clients import ClientPool, WaitConfig
    from ..core.document.models import Flag, Metadata, PageCard, Signal, Structure, Transport, XhrCall
    from ..core.client.models import Robots
    from .lazy import LazyDocument, LazyReference, LazyWebClient

T = TypeVar("T")
E = TypeVar("E", bound="Event")  # an event subtype, for events_of(cls) -> list[cls]


# The eager surfaces ARE the cores: ``Reference`` / ``Document`` / ``WebClient`` /
# ``Session`` are the core classes themselves (imported above) -- each implements its
# verbs+fields via the ``I<Core>`` interface it inherits, and its typed accessors
# (``pool``/``bus``/``session``/``lazy``) live on the core. Only the async / lazy
# views are distinct typed stubs over the same cores.
if TYPE_CHECKING:

    class AsyncReference(Reference):
        """The async view of a :class:`Reference`: ``resolve`` is awaitable
        (``await ref.resolve()``); every other op is the same in-memory surface,
        and Core-returning ops stay in the async tier. A pure typing stub: at
        runtime ``AsyncReference is Reference`` -- the async-ness comes from the
        bound client's dispatcher (``_mode``), not the type."""

        @property
        def lazy(self) -> "LazyReference": ...

        # >>> generated: AsyncReference surface <<<
        # fmt: off
        @property
        def url(self) -> str: ...
        def join(self, href: str) -> "AsyncReference": ...
        def replace(self, **fields: Any) -> "AsyncReference": ...
        async def resolve(self, *, browser: "bool | Literal['never', 'auto', 'always']" = ..., policy: 'dict[str, Any] | None' = ..., optional: bool = ..., error: Any = ..., keep_alive: 'bool | float' = ..., wait: 'WaitConfig | None' = ...) -> "AsyncDocument": ...  # type: ignore[override]
        def with_params(self, **params: str) -> "AsyncReference": ...
        # fmt: on
        # >>> end generated <<<

    class AsyncDocument(Document):
        """The async view of a :class:`Document`: in-memory ops are synchronous;
        Core-returning ops stay in the async tier so a later IO op is awaitable
        (``await doc.select('a').attr('href').resolve()``). A pure typing stub: at
        runtime ``AsyncDocument is Document``."""

        @property
        def lazy(self) -> "LazyDocument": ...

        # >>> generated: AsyncDocument surface <<<
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
        def region(self) -> str: ...
        @property
        def text_content(self) -> str | None: ...
        @property
        def title(self) -> str | None: ...
        def anti_bot_present(self) -> "Flag": ...
        def anti_bot_triggered(self) -> "Flag": ...
        def as_json(self) -> "AsyncDocument": ...
        @overload
        def attr(self, name: Literal['href', 'src', 'action']) -> "AsyncReference": ...  # type: ignore[overload-overlap]
        @overload
        def attr(self, name: str, *, optional: bool = ..., error: Any = ...) -> "Field[str]": ...
        def buttons(self) -> "Flag": ...
        def card(self) -> "PageCard": ...
        async def click(self, selector: str | None = ..., *, timeout: float | None = ..., optional: bool = ..., error: Any = ...) -> "AsyncDocument": ...  # type: ignore[override]
        def elements(self) -> "list[Element]": ...
        async def evaluate(self, script: str, *, mutates: bool = ...) -> "Any": ...  # type: ignore[override]
        @overload
        def events_of(self, event_type: type[E]) -> "list[E]": ...
        @overload
        def events_of(self, event_type: str) -> "list[Event]": ...
        def flags(self) -> "list[Flag]": ...
        def forms(self) -> "Flag": ...
        def framework(self) -> "str | None": ...
        def html(self) -> "str": ...
        def iframe(self) -> "Flag": ...
        def is_empty(self) -> "Field[bool]": ...
        def is_ok(self) -> "Field[bool]": ...
        def large_document(self) -> "Flag": ...
        def links(self) -> "Collection[AsyncReference]": ...
        def login_present(self) -> "Flag": ...
        def login_required(self) -> "Flag": ...
        def markdown(self, *, main_content_only: bool = ...) -> "str": ...
        def metadata(self) -> "Metadata": ...
        def pagination(self) -> "Flag": ...
        def ref(self) -> "AsyncReference": ...
        def regex(self, pattern: str, *, group: int | str = ..., flags: str = ...) -> "Field[str]": ...
        def regex_all(self, pattern: str, *, group: int | str = ..., flags: str = ...) -> "list[str]": ...
        async def reload(self) -> "AsyncDocument": ...  # type: ignore[override]
        @overload
        def render(self, format: Literal['elements']) -> "list[Element]": ...
        @overload
        def render(self, format: Literal['links']) -> "Collection[AsyncReference]": ...
        @overload
        def render(self, format: str, **options: Any) -> "str": ...
        async def screenshot(self, selector: str | None = ...) -> "AsyncDocument": ...  # type: ignore[override]
        def select(self, selector: str, *, index: int = ..., optional: bool = ..., error: Any = ...) -> "AsyncDocument": ...
        def select_all(self, selector: str, *, limit: int | None = ..., offset: int = ...) -> "Collection[AsyncDocument]": ...
        def shadow_dom(self) -> "Flag": ...
        def skeleton(self, *, max_lines: int = ..., text_chars: int = ..., max_depth: int = ..., max_siblings: int = ..., legend: bool = ..., collapse: bool = ..., drop_chrome: bool = ..., annotate_origin: bool = ...) -> "str": ...
        def spa(self) -> "Flag": ...
        def structure(self) -> "Structure": ...
        def text(self, *, main_content_only: bool = ...) -> "str": ...
        def transport(self) -> "Transport": ...
        async def wait_for(self, selector: str | None = ..., *, timeout: float | None = ..., optional: bool = ..., error: Any = ...) -> "AsyncDocument": ...  # type: ignore[override]
        async def write(self, selector: str, text: str, *, timeout: float | None = ..., optional: bool = ..., error: Any = ...) -> "AsyncDocument": ...  # type: ignore[override]
        def xhr_endpoints(self) -> "list[XhrCall]": ...
        # fmt: on
        # >>> end generated <<<

else:  # at runtime the async view IS the core (dispatch mode, not a subtype)
    AsyncReference = Reference
    AsyncDocument = Document

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
    return _core_from_url(url, method, params, headers, cookies)


def default_client() -> "WebClient":
    """The process-local shared client (recreated after close) -- the surface over
    the one engine every unbound operation shares (see ``core.client``)."""
    from ..core.client import default_client as _default

    return _default()


if TYPE_CHECKING:

    class AsyncWebClient(WebClient):
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
        async def fetch(self, url: Any, *, browser: "bool | Literal['never', 'auto', 'always']" = ..., optional: bool = ..., error: Any = ..., keep_alive: 'bool | float' = ..., wait: 'WaitConfig | None' = ..., **kw: Any) -> "AsyncDocument": ...  # type: ignore[override]
        def ref(self, url: Any, method: str = ..., **kw: Any) -> "AsyncReference": ...
        async def robots(self, url: Any) -> "Robots": ...  # type: ignore[override]
        async def sitemap(self, url: Any, *, limit: int = ...) -> "Collection[AsyncReference]": ...  # type: ignore[override]
        # fmt: on
        # >>> end generated <<<

else:  # at runtime the async client is the core in async-dispatcher mode
    AsyncWebClient = _async_client


def RemoteWebClient(url: str, token: str | None = None) -> "WebClient":
    """A ``WebClient`` over a remote core -- literally the same eager surface, run
    server-side. A factory, not a subclass: the remote-ness is entirely in the
    core (``RemoteWebClientCore``), a different-dispatcher ``WebClient`` whose
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
