"""The detection Context: everything a detector may read, gathered once.

A detector never touches a core, an HTTP response, or a browser page directly -- it
reads this immutable snapshot. Cheap request/static fields are always filled;
``tree`` / ``events`` / ``render_stats`` are filled only by the facet (a browser
render), so a rendered/network detector self-skips when they are absent.

Pure (no cores, no lxml) so the request/static half runs on a remote resolve too.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

_TAGS = re.compile(r"<(script|style)\b[^>]*>.*?</\1>", re.I | re.S)
_ANYTAG = re.compile(r"<[^>]+>")


def norm(text: str) -> str:
    """Collapse whitespace to single spaces (a shared helper, no lxml)."""
    return " ".join(text.split())


def visible_text(html: str) -> str:
    """Rough visible text: drop script/style, strip tags, collapse whitespace."""
    return " ".join(_ANYTAG.sub(" ", _TAGS.sub(" ", html)).split())


def _lower_headers(headers: "Mapping[Any, Any] | Iterable[tuple[Any, Any]]") -> dict[str, str]:
    items = headers.items() if isinstance(headers, Mapping) else headers
    return {str(k).lower(): str(v).lower() for k, v in items}


@dataclass(frozen=True)
class Context:
    """An immutable snapshot of a resolved response for the detectors to read."""

    status: int = 0
    headers: dict[str, str] = field(default_factory=dict)  # lowercased keys + values
    cookies: list[str] = field(default_factory=list)  # cookie names
    text: str = ""  # the decoded body
    low: str = ""  # text.lower()[:8000]
    is_html: bool = False
    visible: str = ""  # visible text (tags/script stripped)
    redirect_chain: list[str] = field(default_factory=list)  # lowercased URLs
    url: str = ""
    final_url: str = ""
    #: filled only by the facet (a browser render) -- request/static detection leaves
    #: them empty and the rendered/network detectors self-skip.
    tree: Any = None  # an lxml root element, or None
    events: list[Any] = field(default_factory=list)  # DOM/network events
    render_stats: dict[str, Any] = field(default_factory=dict)

    @property
    def header_blob(self) -> str:
        return " ".join(self.headers.keys()) + " " + " ".join(self.headers.values())

    @property
    def cookie_blob(self) -> str:
        return " ".join(str(c).lower() for c in self.cookies)

    @classmethod
    def from_response(
        cls,
        status: int,
        headers: "Mapping[Any, Any] | Iterable[tuple[Any, Any]]",
        cookies: "Iterable[str] | Mapping[str, Any]",
        body: "bytes | str | None",
        redirect_chain: "Iterable[str]" = (),
        *,
        url: str = "",
        final_url: str = "",
        tree: Any = None,
        events: "Iterable[Any]" = (),
        render_stats: "Mapping[str, Any] | None" = None,
    ) -> "Context":
        """Build a Context from a raw response (+ optional facet inputs). Decodes the
        body and derives the cheap text fields once, so every detector shares them."""
        text = body.decode("utf-8", "replace") if isinstance(body, (bytes, bytearray)) else (body or "")
        hmap = _lower_headers(headers)
        low = text.lower()[:8000]
        is_html = "html" in hmap.get("content-type", "") or "<html" in low or "<!doctype html" in low
        cookie_names = list(cookies.keys()) if isinstance(cookies, Mapping) else list(cookies)
        return cls(
            status=status,
            headers=hmap,
            cookies=cookie_names,
            text=text,
            low=low,
            is_html=is_html,
            visible=visible_text(text) if is_html else text,
            redirect_chain=[str(u).lower() for u in redirect_chain],
            url=url,
            final_url=final_url,
            tree=tree,
            events=list(events),
            render_stats=dict(render_stats or {}),
        )


__all__ = ["Context", "norm", "visible_text"]
