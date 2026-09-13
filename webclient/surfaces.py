"""Concrete eager surfaces. The class scaffolding is hand-written; the op
signature blocks (marked ``>>> generated <<<``) are produced by
``scripts.gen_stubs`` from each core's fields + its backings' typed ops."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal, TypeVar, overload

from pydantic import BaseModel

from .core.client_core import WebClientCore
from .core.document_core import DocumentCore
from .core.reference_core import HttpMethod, ReferenceCore
from .core.reference_core import from_url as _core_from_url
from .surface import Surface, surface

if TYPE_CHECKING:
    from .collection import Collection, Field
    from .core.document_core import Element
    from .models import Lazy, LazyDocument, LazyField, LazyReference

T = TypeVar("T")


@surface(ReferenceCore)
class Reference(Surface):
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
class Document(Surface):
    """A resolved document (eager): ``select``/``select_all``/``attr``/``text``/
    ``render``/events, plus the live interaction set when backed by a page.
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
        def text(self) -> str: ...
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
        def ref(self) -> "Reference": ...
        def reload(self) -> "Document": ...
        @overload
        def render(self, format: Literal['elements']) -> "list[Element]": ...
        @overload
        def render(self, format: Literal['links']) -> "Collection[Reference]": ...
        @overload
        def render(self, format: str, **options: Any) -> "str": ...
        def screenshot(self, selector: str | None = ...) -> "Document": ...
        def select(self, selector: str, *, index: int = ..., error: Any = ...) -> "Document": ...
        def select_all(self, selector: str, *, limit: int | None = ..., offset: int = ...) -> "Collection[Document]": ...
        def summary(self) -> "dict[str, Any]": ...
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


class SearchEngine(BaseModel):
    """A configurable search backend: a URL template (``{q}`` = the query) and
    the selectors that pick each result's title and link out of the page."""

    url: str
    result: str = ".result"
    title: str = "a"
    link: str = "a"


class Session:
    """A logical identity (cookies/headers/ttl) spanning fetches. ``ref`` and
    ``fetch`` return lazy references bound to this session."""

    def __init__(self, core: Any) -> None:
        self._core = core

    def ref(self, url: str, method: HttpMethod = "get", **kw: Any) -> Any:
        from .expr import Expr
        from .plan import Plan

        spec = _core_from_url(url, method, **kw).model_dump()
        return Expr(Plan(root="Reference", source=spec), self._core)

    def fetch(self, url: str, **kw: Any) -> Any:
        return self.ref(url, **kw).resolve()

    def close(self) -> None:
        self._core.close()

    def document(self, name: str) -> Any:
        """Recover a document from this session's scope, or ``None``."""
        from .surface import wrap

        core = self._core.document(name)
        return wrap(core) if core is not None else None

    @property
    def id(self) -> str:
        return self._core.id

    @property
    def status(self) -> str:
        return self._core.status

    @property
    def expires_at(self) -> Any:
        return self._core.expires_at

    @property
    def cookies(self) -> dict[str, str]:
        return self._core.cookies


class _ClientBase:
    """The shared plan-building surface. ``WebClient`` and ``AsyncWebClient``
    build the same lazy plans off the same ``WebClientCore``; they differ only
    in how ``execute`` runs (sync bridge vs awaited off-thread)."""

    _core: WebClientCore

    def __init__(self, core: WebClientCore | None = None, **policy: Any) -> None:
        self._core = core if core is not None else WebClientCore(**policy)

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

    def use(self, renderer: Renderer) -> Any:
        self._core.use(renderer)
        return self

    def ref(self, url: Any, method: HttpMethod = "get", **kw: Any) -> "LazyReference":
        """A lazy client-bound reference: ``.ref(url).resolve()...`` (statically
        a ``LazyReference``; at runtime an Expr recording a plan). ``url`` may be
        a URL string, a ``Reference``, or a ``ReferenceCore``."""
        from .expr import Expr
        from .plan import Plan

        if isinstance(url, Reference):
            core = url._core
        elif isinstance(url, ReferenceCore):
            core = url
        else:
            core = _core_from_url(url, method, **kw)
        return Expr(Plan(root="Reference", source=core.model_dump()), self._core)

    #: the same bound reference root as ``ref``.
    lazy = ref

    def fetch(
        self, url: str, *, optional: bool = False, error: Any = None, **kw: Any
    ) -> "LazyDocument":
        """A lazy fetch: ``ref(url).resolve()``; run it to materialise."""
        return self.ref(url, **kw).resolve(optional=optional, error=error)

    def session(
        self,
        *,
        ttl: float | None = None,
        headers: dict[str, str] | None = None,
        **kw: Any,
    ) -> Session:
        """A new session sharing this client's engine (a scoped core)."""
        from .core.session_core import WebSessionCore

        core = WebSessionCore(ttl=ttl, session_headers=headers or {}, **kw)
        core.bind(self._core)
        return Session(core)

    def search(self, query: str, *, engine: SearchEngine, limit: int = 10) -> Any:
        """A lazy search plan: resolve the engine's query URL, then extract a
        (title, url) row per result. Run it to get the hits."""
        from .expr import doc

        plan = self.ref(engine.url.format(q=query)).resolve().select_all(engine.result)
        if limit:
            plan = plan.limit(limit)
        return plan.extract(
            title=doc.select(engine.title).attr("text"),
            url=doc.select(engine.link).attr("href"),
        ).project()

    def summary(self, url: str, **kw: Any) -> Any:
        """A lazy plan resolving ``url`` to a title + markdown digest."""
        return self.ref(url, **kw).resolve().summary()

    def document(self, name: str) -> Document | None:
        """Recover a materialised Document by name (same surface object), or
        ``None`` if it is not (or no longer) in scope."""
        from .surface import wrap

        core = self._core.document(name)
        return wrap(core) if core is not None else None

    def reference(self, name: str) -> Reference | None:
        """Recover a Reference by its (root) name, or ``None``."""
        from .surface import wrap

        core = self._core.reference(name)
        return wrap(core) if core is not None else None

    def release(self, doc: Document) -> None:
        """Return a live document's browser page to the pool."""
        self._core.release(doc._core)

    @property
    def pool(self) -> Any:
        """The client's transport-lease pool (``.stats()``)."""
        return self._core.pool


class WebClient(_ClientBase):
    """The synchronous client surface: build lazy plans, run them on the engine
    loop. Owns (or is handed) a ``WebClientCore``."""

    if TYPE_CHECKING:

        @overload
        def execute(self, expr: "Lazy[T]", context: Any = ...) -> T: ...
        @overload
        def execute(
            self, expr: Any, context: Any = ..., *, stream: bool = ...
        ) -> Any: ...

    def execute(
        self, expr: Any, context: Any = None, *, stream: bool = False, **kw: Any
    ) -> Any:
        """Run a recorded lazy plan on this client. A remote core round-trips
        over HTTP; otherwise it runs on the engine loop. ``stream=True`` yields
        rows as they complete."""
        from .collection import Field
        from .executor import evaluate

        if hasattr(self._core, "remote_execute"):
            return self._core.remote_execute(expr, context)
        if stream:
            return self._stream(expr, context)
        result = evaluate(expr, context, client=self._core)
        if isinstance(result, Field):
            return result
        if isinstance(result, (str, int, float, bool)) or result is None:
            return Field(result)  # a scalar leaf -> a Field
        return result

    def _stream(self, expr: Any, context: Any) -> Any:
        """Bridge the async row stream to a sync iterator via the engine loop
        (a ``_pump`` task feeds a bounded queue), publishing plan events."""
        from .collection import Field
        from .events import PlanEvent
        from .executor import astream

        bus = self._core.bus
        bus.publish(PlanEvent(phase="started"))
        count = 0
        for row in self._core.loop().stream(astream(expr, context, client=self._core)):
            count += 1
            bus.publish(PlanEvent(phase="row"))
            yield row.get() if isinstance(row, Field) else row
        bus.publish(PlanEvent(phase="done", detail={"rows": count}))

    def close(self) -> None:
        self._core.close()

    def __enter__(self) -> "WebClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self._core.close()


class AsyncWebClient(_ClientBase):
    """The async client surface: the very same plans as ``WebClient``, awaited.
    Execution runs the (sync) evaluator off the caller's loop so ``await`` does
    not block it."""

    if TYPE_CHECKING:
        from typing import Coroutine

        @overload
        def execute(
            self, expr: "Lazy[T]", context: Any = ...
        ) -> "Coroutine[Any, Any, T]": ...
        @overload
        def execute(
            self, expr: Any, context: Any = ..., *, stream: bool = ...
        ) -> Any: ...

    def execute(
        self, expr: Any, context: Any = None, *, stream: bool = False, **kw: Any
    ) -> Any:
        """Awaited execution (same plans as ``WebClient``). Non-stream returns
        an awaitable; ``stream=True`` returns an async iterator of rows."""
        if stream:
            return self._astream(expr, context)
        return self._aexecute(expr, context)

    async def _run(self, expr: Any, context: Any) -> Any:
        """Await ``aevaluate`` on the engine loop without blocking the caller's
        loop (bridged via ``run_coroutine_threadsafe`` + ``wrap_future``)."""
        import asyncio

        from .executor import aevaluate

        return await asyncio.wrap_future(
            self._core.loop().submit(aevaluate(expr, context, client=self._core))
        )

    async def _aexecute(self, expr: Any, context: Any) -> Any:
        from .collection import Field

        result = await self._run(expr, context)
        if isinstance(result, Field):
            return result
        if isinstance(result, (str, int, float, bool)) or result is None:
            return Field(result)
        return result

    async def _astream(self, expr: Any, context: Any) -> Any:
        from .collection import Collection, Field

        result = await self._run(expr, context)
        for row in (
            list(result) if isinstance(result, (list, Collection)) else [result]
        ):
            yield row.get() if isinstance(row, Field) else row

    async def aclose(self) -> None:
        import asyncio

        await asyncio.to_thread(self._core.close)

    async def __aenter__(self) -> "AsyncWebClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()


__all__ = [
    "Reference",
    "Document",
    "LiveDocument",
    "Session",
    "SearchEngine",
    "WebClient",
    "AsyncWebClient",
    "default_client",
    "from_url",
]
