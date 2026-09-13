"""Surfaces & plugins: the one pathway for all capture and rendering.

A Surface is an attachment point (an instance of something observable); a
Plugin declares which surface kinds it instruments and emits events through
the surface handle. The engine creates one Surface per (instance, plugin)
so ``emit`` can stamp the plugin's name.
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, PrivateAttr

from ..events import Event
from ..core.document import Document, Script

SurfaceKind = Literal["client", "session", "transport", "page", "document",
                      "node", "plan"]


class Surface(BaseModel):
    """Handle a plugin receives on attach. ``emit`` publishes to the bus,
    OVERWRITING correlation ids with the surface's own and setting
    ``source`` to the plugin's name (ISSUES #15). ``node_id`` is left as
    the plugin stamped it -- only the plugin knows the element."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    kind: SurfaceKind
    raw: Any = None
    session_id: str | None = None
    document_id: str | None = None
    plan_id: str | None = None

    _bus: Any = PrivateAttr(default=None)
    _source: str = PrivateAttr(default="core")

    def emit(self, event: Event) -> None:
        event.session_id = self.session_id
        event.document_id = self.document_id
        event.plan_id = self.plan_id
        event.source = self._source
        self._bus.publish(event)


class Plugin(BaseModel):
    """Base class for instrumentation plugins. Register with
    ``WebClient.use``; the engine calls ``attach`` when a surface instance
    of a kind in ``surfaces`` is created and ``detach`` when it goes away."""

    name: str
    version: str = "0"
    surfaces: list[SurfaceKind] = []
    events: list[type[Event]] = []
    scripts: list[Script] = []       # injected into "page" surfaces (M4)

    def attach(self, surface: Surface) -> None: ...

    def detach(self, surface: Surface) -> None: ...


class Renderer(Plugin):
    """A plugin providing named representations for one document kind.
    ``surfaces`` is implicitly ["document"]; ``render`` is pure -- computed
    from the parsed document, only the result crosses any wire."""

    kind: Literal["html", "json", "xml", "binary"] = "html"
    formats: list[str] = []

    def render(self, document: Document, format: str, **options: Any) -> Any:
        raise NotImplementedError
