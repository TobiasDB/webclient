"""WebClientCore: the engine core (MVP).

Owns the engine loop + an http client; its backings are the client's verbs
(fetch for now; search/crawl/session later). Everything async; the sync
surface bridges onto the loop. Remote will swap ``aexecute`` for a round-trip.
"""
from __future__ import annotations

from typing import Any, ClassVar

import httpx
from pydantic import BaseModel, ConfigDict, PrivateAttr

from ..engine import http as engine_http
from ..engine.loop import EngineLoop
from .document_core import DocumentCore
from .reference_core import ReferenceCore
from .web_core import Backing, WebCore


class WebClientCore(WebCore, BaseModel):
    """Core Fields (policy) + engine loop + http client. The user-facing
    ``WebClient`` is the surface; this is the machinery it drives."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    timeout: float = 30.0
    default_headers: dict[str, str] = {}

    _loop: Any = PrivateAttr(default=None)
    _http: Any = PrivateAttr(default=None)     # httpx.AsyncClient (MVP: one shared)
    _closed: bool = PrivateAttr(default=False)

    BACKINGS: ClassVar[tuple[Backing, ...]] = ()   # TODO: (Fetch, Search, ...) as ops

    # -- loop / lifecycle ----------------------------------------------------
    def loop(self) -> EngineLoop:
        if self._loop is None:
            self._loop = EngineLoop()
        return self._loop

    async def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(follow_redirects=True)
        return self._http

    def close(self) -> None:
        if self._closed:
            return
        if self._loop is not None and not self._loop.closed and self._http is not None:
            self._loop.run(self._http.aclose())
        self._closed = True
        if self._loop is not None:
            self._loop.stop()

    # -- fetch (resolve a ReferenceCore -> DocumentCore) ---------------------
    async def afetch(self, ref: ReferenceCore, *, optional: bool = False) -> DocumentCore:
        client = await self._client()
        headers = {**self.default_headers, **ref.headers}
        resp = await engine_http.request(
            client, ref, headers=headers, cookies=ref.cookies,
            timeout=self.timeout, retries=0)
        kind = engine_http.sniff_kind(resp.headers.get("content-type"), resp.content)
        doc = DocumentCore(
            url=ref.dispatch("url"), final_url=str(resp.url), kind=kind,
            content=resp.content, status_code=resp.status_code,
            response_headers=dict(resp.headers),
            encoding=engine_http.charset_of(resp.headers.get("content-type")))
        doc._client = self
        return doc

    def fetch(self, ref: ReferenceCore, *, optional: bool = False) -> DocumentCore:
        """Sync: run ``afetch`` on the engine loop."""
        return self.loop().run(self.afetch(ref, optional=optional))


__all__ = ["WebClientCore"]
