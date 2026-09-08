"""WebClient: lifecycle root and facade.

Owns the ClientPool, EventBus, EventRegistry, plugins and the document
registry. M2 scope: http fetch, plugin attach/detach, renderer table,
default client. Sessions land in M3, browser in M4, plans in M6.
"""
from __future__ import annotations

import atexit
import logging
import threading
import weakref
from typing import Any, Literal, Sequence
from uuid import uuid4

import httpx
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr
from typing_extensions import Self

from .engine import http as engine_http
from .engine.loop import EngineLoop
from .events import EventBus, EventRegistry
from .models import (
    Document,
    FetchError,
    HttpMethod,
    Proxy,
    Reference,
    Script,
)
from .plugins.base import Plugin, Renderer, Surface, SurfaceKind
from .plugins.network import HttpNetworkPlugin
from .plugins.render import core_renderers
from .pool import ClientPool

logger = logging.getLogger("webclient")


def _core_plugins() -> list[Plugin]:
    return [HttpNetworkPlugin(), *core_renderers()]


class WebClient(BaseModel):
    """Lifecycle root. References, Documents and LiveDocuments are bound to
    the WebClient that made them; closing it reclaims everything."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    # policy
    timeout: float = 30.0
    retries: int = 0                 # transport errors only (ISSUES #20)
    verify_tls: bool = True
    default_headers: dict[str, str] = Field(default_factory=dict)
    proxy_pool: list[Proxy] = Field(default_factory=list)
    headless: bool = True
    default_scripts: list[Script] = Field(default_factory=list)

    # owned infrastructure (default_factory: these hold runtime state)
    pool: ClientPool = Field(default_factory=ClientPool)
    bus: EventBus = Field(default_factory=EventBus)
    registry: EventRegistry = Field(default_factory=EventRegistry)
    plugins: list[Plugin] = Field(default_factory=list)

    _loop: EngineLoop | None = PrivateAttr(default=None)
    _loop_lock: Any = PrivateAttr(default_factory=threading.Lock)
    _documents: dict[str, Any] = PrivateAttr(default_factory=dict)
    _render_table: dict[tuple[str, str], Renderer] = PrivateAttr(default_factory=dict)
    _closed: bool = PrivateAttr(default=False)

    def model_post_init(self, __context: Any) -> None:
        self.pool._owner = self
        for plugin in _core_plugins():
            self.use(plugin)

    # -- lifecycle -----------------------------------------------------------
    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._loop is not None and not self._loop.closed:
            self._loop.run(self.pool._aclose())
            self._loop.stop()

    def _ensure_loop(self) -> EngineLoop:
        if self._closed:
            raise RuntimeError("WebClient is closed")
        with self._loop_lock:
            if self._loop is None:
                self._loop = EngineLoop()
        return self._loop

    # -- plugins -------------------------------------------------------------
    def use(self, plugin: Plugin) -> Self:
        """Register a plugin: same name replaces; a Renderer claiming an
        occupied (kind, format) shadows it with a warning (ISSUES #16)."""
        self.plugins[:] = [p for p in self.plugins if p.name != plugin.name]
        self.plugins.append(plugin)
        for event_cls in plugin.events:
            self.registry.register(event_cls)
        if isinstance(plugin, Renderer):
            for fmt in plugin.formats:
                key = (plugin.kind, fmt)
                holder = self._render_table.get(key)
                if holder is not None and holder.name != plugin.name:
                    logger.warning(
                        "renderer %r shadows %r for %s", plugin.name,
                        holder.name, key)
                self._render_table[key] = plugin
        return self

    def _attach(self, kind: SurfaceKind, raw: Any, *,
                session_id: str | None = None,
                document_id: str | None = None,
                plan_id: str | None = None) -> list[tuple[Plugin, Surface]]:
        attached = []
        for plugin in self.plugins:
            if kind not in plugin.surfaces:
                continue
            surface = Surface(kind=kind, raw=raw, session_id=session_id,
                              document_id=document_id, plan_id=plan_id)
            surface._bus = self.bus
            surface._source = plugin.name
            plugin.attach(surface)
            attached.append((plugin, surface))
        return attached

    @staticmethod
    def _detach(attached: list[tuple[Plugin, Surface]]) -> None:
        for plugin, surface in reversed(attached):
            plugin.detach(surface)

    # -- references / fetching ----------------------------------------------
    def ref(self, url: str, method: HttpMethod = "get",
            **kwargs: Any) -> Reference:
        return Reference.from_url(url, method=method, **kwargs).bind(self)

    def fetch(self, ref: Reference, *, browser: bool = False,
              session: Any = None,
              scripts: Sequence[Script] | None = None,
              wait_until: str = "load",
              optional: bool = False) -> Document:
        if browser:
            raise NotImplementedError("browser fetch lands in M4")
        if session is not None:
            raise NotImplementedError("sessions land in M3")
        return self._ensure_loop().run(self._fetch(ref, optional=optional))

    async def _fetch(self, ref: Reference, *, optional: bool) -> Document:
        document_id = uuid4().hex
        routed: list[Any] = []
        subscription = self.bus.subscribe("", routed.append,
                                          document_id=document_id)
        lease = await self.pool._acquire("http")
        try:
            attached = self._attach("transport", lease._client,
                                    document_id=document_id)
            try:
                response = await engine_http.request(
                    lease._client, ref,
                    default_headers=self.default_headers,
                    timeout=self.timeout, retries=self.retries)
            finally:
                self._detach(attached)
        except httpx.TransportError as exc:
            subscription.cancel()
            if optional:
                return self._build_document(ref, document_id, None, routed)
            raise FetchError(f"{ref.method.upper()} {ref.url}: {exc}") from exc
        finally:
            await self.pool._release(lease)
        subscription.cancel()
        document = self._build_document(ref, document_id, response, routed)
        if not optional and not document.ok:
            raise FetchError(
                f"{ref.method.upper()} {ref.url} -> {document.status_code}",
                document=document)
        return document

    def _build_document(self, ref: Reference, document_id: str,
                        response: httpx.Response | None,
                        routed: list[Any]) -> Document:
        fields = {name: getattr(ref, name) for name in Reference.model_fields}
        if response is None:
            document = Document(**fields, id=document_id, status_code=0)
        else:
            content_type = response.headers.get("content-type")
            document = Document(
                **fields,
                id=document_id,
                kind=engine_http.sniff_kind(content_type, response.content),
                content=response.content,
                status_code=response.status_code,
                response_headers=dict(response.headers),
                encoding=engine_http.charset_of(content_type),
                final_url=str(response.url),
                elapsed=response.elapsed.total_seconds(),
            )
        document._client = self
        document.events.extend(routed)
        self._documents[document_id] = weakref.ref(document)
        self._attach("document", document, document_id=document_id)
        return document

    # -- registries ----------------------------------------------------------
    def document(self, document_id: str) -> Document | None:
        found = self._documents.get(document_id)
        return found() if found is not None else None

    # -- later milestones -----------------------------------------------------
    def session(self, **overrides: Any) -> Any:
        raise NotImplementedError("sessions land in M3")

    def release(self, doc: Any) -> None:
        raise NotImplementedError("live documents land in M4")

    def execute(self, plan: Any, context: Any, *, stream: bool = False) -> Any:
        raise NotImplementedError("plan execution lands in M6")


# --------------------------------------------------------------------------- #
# Process-wide default client (ISSUES #17)
# --------------------------------------------------------------------------- #

_default: WebClient | None = None
_default_lock = threading.Lock()


def default_client() -> WebClient:
    """Lazily-created process default; recreated after close; closed
    best-effort at interpreter exit."""
    global _default
    with _default_lock:
        if _default is None or _default._closed:
            _default = WebClient()
        return _default


@atexit.register
def _close_default() -> None:
    with _default_lock:
        if _default is not None and not _default._closed:
            try:
                _default.close()
            except Exception:  # best-effort teardown only
                pass
