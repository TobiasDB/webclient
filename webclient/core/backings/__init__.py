"""Backings package: the switch and the per-medium providers."""
from __future__ import annotations

from typing import TYPE_CHECKING

from ..base import Capability
from .base import Backing, pw_selector
from .html import HtmlBacking
from .json import JsonBacking
from .live import LiveAction, LiveSelect

if TYPE_CHECKING:
    from ..document import DocumentCore

_HTML = HtmlBacking()
_JSON = JsonBacking()
_LIVE_SELECT = LiveSelect()
_LIVE_ACTION = LiveAction()


def choose(core: "DocumentCore") -> list[Backing]:
    """Order matters: the first backing that provides an op wins. A live page
    gets HtmlBacking too, so ``render`` works on the current snapshot while
    ``select``/``attr`` stay live."""
    if core.page is not None or core.locator is not None:
        return [_LIVE_SELECT, _LIVE_ACTION, _HTML]
    if core.doc.kind == "json":
        return [_JSON]
    if core.doc.kind in ("html", "xml"):
        return [_HTML]
    return []


GATES: dict[str, Capability] = {
    "select": "tree", "select_all": "tree", "attr": "tree", "render": "tree",
    **{op: "page" for op in _LIVE_ACTION.provides}}

__all__ = ["Backing", "HtmlBacking", "JsonBacking", "LiveSelect", "LiveAction",
           "choose", "GATES", "pw_selector"]
