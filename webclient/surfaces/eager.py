"""Concrete eager surfaces. The class scaffolding is hand-written; the op
signature blocks (marked ``>>> generated <<<``) are produced by
``scripts.gen_stubs`` from each core's fields + its backings' typed ops."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal, TypeVar, cast, overload

from ..core.client import WebClientCore
from ..core.document import DocumentCore
from ..core.reference import HttpMethod, ReferenceCore
from ..core.reference import from_url as _core_from_url
from ..core.session import WebSessionCore

if TYPE_CHECKING:
    from ..collection import Collection, Field
    from ..core.document import Element
    from ..summary import Metadata, Runtime, Structure, Summary, Transport
    from .lazy import Lazy, LazyDocument, LazyReference, LazyWebClient

T = TypeVar("T")


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
        def action_events(self) -> list[Any]: ...
        @property
        def console(self) -> list[Any]: ...
        @property
        def dom_mutations(self) -> list[Any]: ...
        @property
        def events(self) -> list[Any]: ...
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
        def events_of(self, event_type: Any) -> "list[Any]": ...
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

else:  # at runtime a surface IS its core
    Reference = ReferenceCore
    Document = DocumentCore
    WebClient = WebClientCore
    Session = WebSessionCore

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


class _ClientBase:
    """The async client's lazy recorder base over a ``WebClientCore``. It has no
    verb bodies: ``__getattr__`` records each authoring verb into a ``WebClient``-
    rooted plan (the executor dispatches the eager backing when the plan runs),
    realized by ``await ...acollect()`` / ``.astream()``. The generated stubs give
    the verbs their types; session / recovery / lifecycle are wrappers here.

    (The *sync* client is the eager ``WebClientCore`` itself -- ``WebClient`` --
    which resolves each verb immediately; this deferred base is what the async
    client's ``await`` needs.)"""

    _core: WebClientCore

    def __init__(self, core: WebClientCore | None = None, **policy: Any) -> None:
        self._core = core if core is not None else WebClientCore(**policy)

    if TYPE_CHECKING:
        # >>> generated: WebClient surface <<<
        # fmt: off
        def fetch(self, url: Any, *, optional: bool = ..., error: Any = ..., **kw: Any) -> "LazyDocument": ...
        def ref(self, url: Any, method: str = ..., **kw: Any) -> "LazyReference": ...
        def summary(self, url: Any, *include: str, **kw: Any) -> "Lazy[Summary]": ...
        # fmt: on
        # >>> end generated <<<
    else:

        def __getattr__(self, name: str) -> Any:  # a verb -> a recorded plan
            if name.startswith("_"):
                raise AttributeError(name)
            core = object.__getattribute__(self, "_core")
            if name in type(core).ops():
                from ..query.expr import Expr
                from ..query.plan import Plan

                return getattr(Expr(Plan(root="WebClient"), core), name)
            raise AttributeError(name)

    @property
    def lazy(self) -> "LazyWebClient":
        """A lazy recorder bound to this client: ``wc.lazy.fetch(url)`` records a
        ``WebClient``-rooted plan on this client's core, run by ``.collect()``."""
        from ..query.expr import lazy_root

        return cast("LazyWebClient", lazy_root(self._core))

    @property
    def core(self) -> WebClientCore:
        """The underlying engine core."""
        return self._core

    def _ensure_loop(self) -> Any:
        """The engine loop (drives async fan-out / bridges sync callers)."""
        return self._core.loop()

    @property
    def _scope(self) -> Any:
        return self._core._scope

    @property
    def _closed(self) -> bool:
        return getattr(self._core, "_closed", False)

    @property
    def bus(self) -> Any:
        """The client's event bus (subscribe to network/dom/console topics)."""
        return self._core.bus

    @property
    def pool(self) -> Any:
        """The client's transport-lease pool (``.stats()``)."""
        return self._core.pool

    def use(self, renderer: Renderer) -> Any:
        self._core.use(renderer)
        return self

    def session(self, **kw: Any) -> Any:
        """A new session sharing this client's engine -- a scoped core (which IS
        its own surface) or a remote session handle."""
        return self._core.session(**kw)

    def document(self, name: str) -> Document | None:
        """Recover a materialised Document by name (same surface object), or
        ``None`` if it is not (or no longer) in scope."""
        from ._base import wrap

        core = self._core.document(name)
        return wrap(core) if core is not None else None

    def reference(self, name: str) -> Reference | None:
        """Recover a Reference by its (root) name, or ``None``."""
        from ._base import wrap

        core = self._core.reference(name)
        return wrap(core) if core is not None else None

    def release(self, doc: Document) -> None:
        """Return a live document's browser page to the pool."""
        self._core.release(doc)


class AsyncWebClient(_ClientBase):
    """The async client surface: the very same plans as ``WebClient``, realised
    with ``await plan.acollect()`` / ``plan.astream()`` on the engine loop off
    the caller's loop."""

    async def aclose(self) -> None:
        import asyncio

        await asyncio.to_thread(self._core.close)

    async def __aenter__(self) -> "AsyncWebClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()


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
    "LiveDocument",
    "Session",
    "WebClient",
    "AsyncWebClient",
    "RemoteWebClient",
    "default_client",
    "from_url",
]
