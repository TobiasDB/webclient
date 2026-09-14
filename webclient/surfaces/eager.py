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
from ._base import Surface, surface

if TYPE_CHECKING:
    from ..collection import Collection, Field
    from ..core.document import Element
    from ..summary import Metadata, Runtime, Structure, Summary, Transport
    from .lazy import Lazy, LazyDocument, LazyReference

T = TypeVar("T")


@surface(ReferenceCore)
class Reference(Surface[ReferenceCore]):
    """A request spec (eager): ``url``/``with_params``/``replace``/``join``/
    ``resolve``. Construct from a core (``Reference(core)``) or directly from
    spec fields (``Reference(hostname=..., path=...)``)."""

    def __init__(self, core: Any = None, **fields: Any) -> None:
        if not isinstance(core, ReferenceCore):
            core = ReferenceCore(**fields)
        super().__init__(core)

    # -- serialisation proxies (a Reference is a request spec on the wire) ---
    def model_dump(self, **kw: Any) -> Any:
        return self._core.model_dump(**kw)

    def model_dump_json(self, **kw: Any) -> Any:
        return self._core.model_dump_json(**kw)

    @classmethod
    def model_validate(cls, data: Any, **kw: Any) -> "Reference":
        return cls(ReferenceCore.model_validate(data, **kw))

    @classmethod
    def model_validate_json(cls, data: Any, **kw: Any) -> "Reference":
        return cls(ReferenceCore.model_validate_json(data, **kw))

    if TYPE_CHECKING:
        # >>> generated: Reference eager surface <<<
        # fmt: off
        kind: str
        name: str
        root: str
        hostname: str
        method: str
        scheme: str
        port: int | None
        path: str
        fragment: str
        params: Any
        headers: Any
        cookies: Any
        body: bytes | None
        json_body: Any
        form: Any
        follow_redirects: bool
        timeout: float | None
        actions: Any
        @property
        def ok(self) -> bool: ...
        @property
        def url(self) -> str: ...
        def join(self, href: str) -> "Reference": ...
        def replace(self, **fields: Any) -> "Reference": ...
        def resolve(self, *, browser: bool = ..., optional: bool = ..., error: Any = ...) -> "Document": ...
        def with_params(self, **params: str) -> "Reference": ...
        # fmt: on
        # >>> end generated <<<


@surface(DocumentCore)
class Document(Surface[DocumentCore]):
    """A resolved document (eager): ``select``/``select_all``/``attr``/
    ``text_content``/``render``/events, plus the live interaction set when backed
    by a page.
    Construct from a core (``Document(core)``) or from core-field kwargs
    (unknown keys are ignored)."""

    def __init__(self, core: Any = None, **fields: Any) -> None:
        if not isinstance(core, DocumentCore):
            known = {k: v for k, v in fields.items() if k in DocumentCore.model_fields}
            core = DocumentCore(**known)
        super().__init__(core)

    if TYPE_CHECKING:
        # >>> generated: Document eager surface <<<
        # fmt: off
        id: str
        name: str
        root: str
        session_id: str
        kind: str
        url: str
        final_url: str | None
        content: bytes
        status_code: int
        response_headers: Any
        encoding: str | None
        elapsed: float | None
        created: float
        accessed: float
        error: Any
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
        def ok(self) -> bool: ...
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


#: A live (browser-backed) document is a Document with the ``page`` capability;
#: the generated tier calls it ``LiveDocument``.
LiveDocument = Document


def from_url(
    url: str,
    method: HttpMethod = "get",
    params: dict[str, str | list[str]] | None = None,
    headers: dict[str, str] | None = None,
    cookies: dict[str, str] | None = None,
) -> Reference:
    """Build a :class:`Reference` from a URL string."""
    return Reference(_core_from_url(url, method, params, headers, cookies))


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


@surface(WebClientCore)
class _ClientDispatch(Surface[WebClientCore]):
    """The eager view the executor uses to run a ``WebClient``-rooted plan: it
    dispatches the client's authoring backings (ref/fetch/summary) to real
    cores. Users always hold the lazy ``WebClient``; this is internal."""


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
        def lazy(self, url: Any, method: str = ..., **kw: Any) -> "LazyReference": ...
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
        self._core.release(doc._core)


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
