"""DocumentCore: the core behind a document (rewrite skeleton).

Core Fields = the resolved response (the single source of truth the surface is
generated from); backings = the per-medium op providers (html / json / live /
events). Talks to the owning WebClientCore for leases / navigation / release.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar, Literal

from pydantic import BaseModel, PrivateAttr

from .web_core import Backing, WebCore
from .reference_core import ReferenceCore

if TYPE_CHECKING:
    from .client_core import WebClientCore


# -- backings (typed methods; the generator reads these signatures) ---------

class HtmlBacking(Backing):
    """Tree ops for html/xml documents. Each op is a typed method -- the
    generator + Expr read ``inspect.signature`` / return hints off these."""

    provides = frozenset({"select", "attr", "text", "links"})
    gate = "tree"

    def applies(self, core: "DocumentCore") -> bool:
        return core.kind in ("html", "xml")

    def select(self, core: "DocumentCore", selector: str, *,
               index: int = 0) -> "DocumentCore": ...          # TODO(port html)

    def attr(self, core: "DocumentCore", name: str) -> str: ...

    def text(self, core: "DocumentCore") -> str: ...

    def links(self, core: "DocumentCore") -> "list[ReferenceCore]": ...


class LiveBacking(Backing):
    """Live-page actions -- only when the core holds a page (capability
    ``page``); its presence is what makes a ``LiveDocument`` subtype."""

    provides = frozenset({"click", "write"})
    gate = "page"

    def applies(self, core: "DocumentCore") -> bool:
        return core._page is not None

    def click(self, core: "DocumentCore", selector: str | None = None,
              ) -> "DocumentCore": ...

    def write(self, core: "DocumentCore", selector: str, text: str,
              ) -> "DocumentCore": ...


class DocumentCore(WebCore, BaseModel):
    """A resolved resource's core. The Core Fields below are what the generated
    ``Document`` / ``LazyDocument`` expose as data; every op (select/attr/
    render/live/events/...) comes from a backing via ``dispatch``."""

    # -- Core Fields (the response; generated onto the surface as data) ------
    id: str = ""
    kind: Literal["html", "json", "xml", "binary"] = "html"
    url: str = ""
    final_url: str | None = None
    content: bytes = b""
    status_code: int = 0
    response_headers: dict[str, str] = {}
    encoding: str | None = None
    # TODO(port): events list, actions chain, options -- from the old Document.

    # -- runtime/machinery (not surface data) --------------------------------
    _client: Any = PrivateAttr(default=None)     # owning WebClientCore
    _page: Any = PrivateAttr(default=None)        # live page lease (M4)
    _tree: Any = PrivateAttr(default=None)        # cached parse
    # TODO(port): locator / lease / routing / identity from the old DocumentCore.

    # -- backings (choose by state; html for a tree, live for a page) --------
    BACKINGS: ClassVar[tuple[Backing, ...]] = (HtmlBacking(), LiveBacking())

    # -- cross-core utility --------------------------------------------------
    # TODO(port): text(), parsed(), element(), snapshot/invalidate, identity --
    #   these are the clean bits of the old DocumentCore, re-homed on backings
    #   where they are really op logic, kept here where they are plumbing.


__all__ = ["DocumentCore"]
