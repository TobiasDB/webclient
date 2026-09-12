"""Core network capture for the http path (ISSUES #21).

Attaches to the "transport" surface (the leased, lease-exclusive httpx
client) and installs a response hook for the request's duration: redirect
hops emit base NetworkEvents, the final response a NavigationEvent. Event
bodies stay empty on the http path -- the Document carries the content.
Browser-side capture (XHR/Fetch/Asset) lands in M4.
"""
from __future__ import annotations

from typing import Any

import httpx
from pydantic import PrivateAttr

from ..events import Event, NavigationEvent, NetworkEvent
from ..core.models import Reference
from .base import Plugin, Surface, SurfaceKind


class HttpNetworkPlugin(Plugin):
    name: str = "core-network"
    surfaces: list[SurfaceKind] = ["transport"]
    events: list[type[Event]] = [NetworkEvent, NavigationEvent]

    _installed: dict[str, Any] = PrivateAttr(default_factory=dict)

    def attach(self, surface: Surface) -> None:
        client: httpx.AsyncClient = surface.raw

        async def on_response(response: httpx.Response) -> None:
            ref = Reference.from_url(str(response.request.url))
            # A 3xx with a Location is a hop the client will follow; the
            # final response (any status) is the navigation. (A 3xx with
            # follow_redirects=False is misclassified as a hop; accepted.)
            is_hop = (300 <= response.status_code < 400
                      and "location" in response.headers)
            cls = NetworkEvent if is_hop else NavigationEvent
            surface.emit(cls(request=ref, status_code=response.status_code))

        hooks = dict(client.event_hooks)
        hooks["response"] = list(hooks.get("response", [])) + [on_response]
        client.event_hooks = hooks
        self._installed[str(id(surface))] = (client, on_response)

    def detach(self, surface: Surface) -> None:
        entry = self._installed.pop(str(id(surface)), None)
        if entry is None:
            return
        client, hook = entry
        hooks = dict(client.event_hooks)
        hooks["response"] = [h for h in hooks.get("response", []) if h is not hook]
        client.event_hooks = hooks
