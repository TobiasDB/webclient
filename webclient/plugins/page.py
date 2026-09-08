"""Core page-surface capture plugins (M4).

- PageNetworkPlugin: response listener -> XHR/Fetch/Navigation/AssetEvent
- PageConsolePlugin: console listener -> ConsoleEvent
- PageDomPlugin: injected MutationObserver script + binding ->
  DOMLoad/Update/Unload/SnapshotEvent, with `node_id` stamped as the
  element's identity path ("n1/n4/n9", ISSUES #9) and periodic full-DOM
  snapshot checkpoints carrying a digest (spec: stream rebuildability).

Plugins needing awaited setup implement `attach_async`, an additive engine
hook (sync `attach` stays the spec surface; ISSUES #29).
"""
from __future__ import annotations

import json
from typing import Any, Literal, cast

from pydantic import PrivateAttr

from ..events import (
    AssetEvent,
    ConsoleEvent,
    DOMLoadEvent,
    DOMSnapshotEvent,
    DOMUnloadEvent,
    DOMUpdateEvent,
    Event,
    FetchEvent,
    NavigationEvent,
    XHREvent,
)
from ..models import Reference, Script
from .base import Plugin, Surface, SurfaceKind

_CONSOLE_LEVELS = {"log": "log", "info": "info", "warning": "warning",
                   "error": "error"}


class PageNetworkPlugin(Plugin):
    name: str = "core-page-network"
    surfaces: list[SurfaceKind] = ["page"]
    events: list[type[Event]] = [XHREvent, FetchEvent, NavigationEvent,
                                 AssetEvent]

    _listeners: dict[int, Any] = PrivateAttr(default_factory=dict)

    def attach(self, surface: Surface) -> None:
        page = surface.raw

        def on_response(response: Any) -> None:
            kind = response.request.resource_type
            ref = Reference.from_url(response.url)
            if kind == "xhr":
                event: Event = XHREvent(request=ref, status_code=response.status)
            elif kind == "fetch":
                event = FetchEvent(request=ref, status_code=response.status)
            elif kind == "document":
                event = NavigationEvent(request=ref, status_code=response.status)
            else:
                event = AssetEvent(request=ref, status_code=response.status,
                                   asset_type=kind)
            surface.emit(event)

        page.on("response", on_response)
        self._listeners[id(surface)] = (page, on_response)

    def detach(self, surface: Surface) -> None:
        entry = self._listeners.pop(id(surface), None)
        if entry is not None:
            entry[0].remove_listener("response", entry[1])


class PageConsolePlugin(Plugin):
    name: str = "core-page-console"
    surfaces: list[SurfaceKind] = ["page"]
    events: list[type[Event]] = [ConsoleEvent]

    _listeners: dict[int, Any] = PrivateAttr(default_factory=dict)

    def attach(self, surface: Surface) -> None:
        page = surface.raw

        def on_console(message: Any) -> None:
            level = cast(Literal["log", "info", "warning", "error"],
                         _CONSOLE_LEVELS.get(message.type, "log"))
            surface.emit(ConsoleEvent(level=level, text=message.text))

        page.on("console", on_console)
        self._listeners[id(surface)] = (page, on_console)

    def detach(self, surface: Surface) -> None:
        entry = self._listeners.pop(id(surface), None)
        if entry is not None:
            entry[0].remove_listener("console", entry[1])


_DOM_CAPTURE_JS = r"""
(() => {
  if (window.__wc) return;
  const wc = window.__wc = { seq: 0, ids: new WeakMap(), updates: 0 };
  wc.idOf = (el) => {
    if (!el || el.nodeType !== 1) el = el && el.parentElement;
    if (!el) return null;
    if (!wc.ids.has(el)) wc.ids.set(el, "n" + (++wc.seq));
    return wc.ids.get(el);
  };
  wc.pathOf = (el) => {
    const parts = [];
    let cur = (el && el.nodeType === 1) ? el : (el && el.parentElement);
    while (cur) { parts.unshift(wc.idOf(cur)); cur = cur.parentElement; }
    return parts.join("/");
  };
  wc.digest = (s) => {
    let h = 5381;
    for (let i = 0; i < s.length; i++) h = ((h << 5) + h + s.charCodeAt(i)) | 0;
    return (h >>> 0).toString(16);
  };
  const emit = (payload) => {
    try { window.__wc_emit(JSON.stringify(payload)); } catch (e) {}
  };
  wc.snapshot = () => {
    const html = document.documentElement ? document.documentElement.outerHTML : "";
    emit({ type: "snapshot", digest: wc.digest(html), html: html });
  };
  addEventListener("DOMContentLoaded", () => {
    emit({ type: "load", node: wc.pathOf(document.body) });
    wc.snapshot();
    const kinds = { childList: "added", attributes: "attribute", characterData: "text" };
    new MutationObserver((muts) => {
      for (const m of muts) {
        let kind = kinds[m.type] || "added";
        if (m.type === "childList" && m.removedNodes.length && !m.addedNodes.length)
          kind = "removed";
        emit({ type: "update", kind: kind, node: wc.pathOf(m.target) });
      }
      wc.updates += muts.length;
      if (wc.updates >= 25) { wc.updates = 0; wc.snapshot(); }
    }).observe(document.documentElement,
               { subtree: true, childList: true, attributes: true, characterData: true });
  });
  addEventListener("beforeunload", () => emit({ type: "unload" }));
})();
"""


class PageDomPlugin(Plugin):
    name: str = "core-page-dom"
    surfaces: list[SurfaceKind] = ["page"]
    events: list[type[Event]] = [DOMLoadEvent, DOMUpdateEvent, DOMUnloadEvent,
                                 DOMSnapshotEvent]
    scripts: list[Script] = [Script(source=_DOM_CAPTURE_JS, run_at="init")]

    # page-id -> current surface: the binding registers once per page but a
    # navigate() swaps document ids, so the handler routes to whatever
    # surface is currently attached for that page.
    _surfaces: dict[int, Surface] = PrivateAttr(default_factory=dict)
    _bound_pages: set[int] = PrivateAttr(default_factory=set)

    def attach(self, surface: Surface) -> None:
        self._surfaces[id(surface.raw)] = surface

    async def attach_async(self, surface: Surface) -> None:
        page = surface.raw
        if id(page) in self._bound_pages:
            return
        self._bound_pages.add(id(page))

        async def on_emit(source: Any, payload_json: str) -> None:
            current = self._surfaces.get(id(page))
            if current is None:
                return
            payload = json.loads(payload_json)
            node_id = payload.get("node")
            kind = payload.get("type")
            if kind == "load":
                event: Event = DOMLoadEvent(node_id=node_id)
            elif kind == "unload":
                event = DOMUnloadEvent(node_id=node_id)
            elif kind == "snapshot":
                event = DOMSnapshotEvent(
                    snapshot={"html": payload.get("html", "")},
                    digest=payload.get("digest", ""))
            else:
                event = DOMUpdateEvent(kind=payload.get("kind", "added"),
                                       node_id=node_id)
            current.emit(event)

        await page.expose_binding("__wc_emit", on_emit)

    def detach(self, surface: Surface) -> None:
        current = self._surfaces.get(id(surface.raw))
        if current is surface:
            del self._surfaces[id(surface.raw)]
