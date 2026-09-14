"""Concrete eager surfaces. The class scaffolding is hand-written; the op
signature blocks (marked ``>>> generated <<<``) are produced by
``scripts.gen_stubs`` from each core's fields + its backings' typed ops."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal, TypeVar, cast, overload

from ..core.client import WebClientCore
from ..core.document import DocumentCore
from ..core.reference import HttpMethod, ReferenceCore
from ..core.reference import from_url as _core_from_url
from ..core.web_core import WebCore

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

else:  # at runtime a surface IS its core
    Reference = ReferenceCore
    Document = DocumentCore

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
    if _DEFAULT is None or _DEFAULT._core._closed:
        _DEFAULT = WebClient()
    return _DEFAULT


class Renderer:
    """A (kind, format) render override -- register with ``wc.use``. Subclass
    and set ``name``/``kind``/``formats`` and implement ``render``."""

    name: str = ""
    kind: str = "html"
    formats: list[str] = []

    def render(self, document: "Document", format: str, **options: Any) -> Any:
        raise NotImplementedError


class Session:
    """A logical identity (cookies/headers/ttl) spanning fetches. ``ref`` and
    ``fetch`` return lazy references bound to this session."""

    def __init__(self, core: Any) -> None:
        self._core = core

    def ref(self, url: str, method: HttpMethod = "get", **kw: Any) -> Any:
        from ..query.expr import Expr
        from ..query.plan import Plan

        spec = _core_from_url(url, method, **kw).model_dump()
        return Expr(Plan(root="Reference", source=spec), self._core)

    def fetch(self, url: str, **kw: Any) -> Any:
        return self.ref(url, **kw).resolve()

    def close(self) -> None:
        self._core.close()

    def __enter__(self) -> "Session":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def document(self, name: str) -> Any:
        """Recover a document from this session's scope, or ``None``."""
        from ._base import wrap

        core = self._core.document(name)
        return wrap(core) if core is not None else None

    @property
    def id(self) -> str:
        return cast(str, self._core.id)

    @property
    def status(self) -> str:
        return cast(str, self._core.status)

    @property
    def expires_at(self) -> Any:
        return self._core.expires_at

    @property
    def cookies(self) -> dict[str, str]:
        return cast("dict[str, str]", self._core.cookies)


class _ClientBase:
    """A thin sync/async/lazy interface over a ``WebClientCore``. It has no verb
    bodies: like ``Document``, the authoring verbs are the core's backings, but
    here the client is *lazy* -- ``__getattr__`` records the call into a
    ``WebClient``-rooted plan (the executor dispatches the eager backing when the
    plan runs). The generated stubs give the verbs their types. The plan is
    realized by ``.collect()``/``.acollect()`` on the recorded handle, which runs
    the core's execute machinery (sync here, awaited in ``AsyncWebClient``, remote
    if the core is a remote subclass); session/recovery/lifecycle are the
    surface's own wrappers."""

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
        """A new session sharing this client's engine. A core-swap decides its
        kind (local ``Session`` surface, or a remote session handle)."""
        made = self._core.session(**kw)
        return Session(made) if isinstance(made, WebCore) else made

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


class WebClient(_ClientBase):
    """The synchronous client surface: build lazy plans and realise them with
    ``.collect()`` / ``.stream()``. Owns (or is handed) a ``WebClientCore``. The
    client has no ``execute`` -- realization goes through the plan, not the
    client (``plan.collect(...)`` calls the core internally)."""

    def close(self) -> None:
        self._core.close()

    def __enter__(self) -> "WebClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self._core.close()


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


def RemoteWebClient(url: str, token: str | None = None) -> WebClient:
    """A ``WebClient`` over a remote core -- literally the same surface, executed
    server-side. A factory, not a subclass: the remote-ness is entirely in the
    core it swaps in (``RemoteWebClientCore``)."""
    from ..core.remote import RemoteWebClientCore

    return WebClient(core=RemoteWebClientCore(url=url, token=token))


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
