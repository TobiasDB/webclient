"""The detection Context: everything a detector may read, gathered once.

A detector never touches a core, an HTTP response, or a browser page directly -- it
reads this immutable snapshot. Cheap request/static fields are always filled;
``tree`` / ``events`` / ``render_stats`` are filled only by the facet (a browser
render), so a rendered/network detector self-skips when they are absent.

Pure (no cores, no lxml) so the request/static half runs on a remote resolve too.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

# from the shared toolkit; norm/visible_text are lxml-free and re-exported so ``context``
# stays remote-safe, and ``parse_html`` is lxml-lazy (degrades to ``None`` without lxml).
from ..dom import norm as norm, parse_html, visible_text as visible_text


def _lower_headers(headers: "Mapping[Any, Any] | Iterable[tuple[Any, Any]]") -> dict[str, str]:
    """Normalise headers to a dict with lowercased keys AND values, so detectors can match
    case-insensitively without re-lowering."""
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
        """All header names and values joined into one string -- a cheap haystack for a detector
        that just wants to substring-search the headers."""
        return " ".join(self.headers.keys()) + " " + " ".join(self.headers.values())

    @property
    def cookie_blob(self) -> str:
        """All cookie names joined (lowercased) into one searchable string."""
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
        # When no facet tree was supplied, parse the static HTML ourselves so tree-based
        # detectors take the accurate cssselect path. ``parse_html`` returns ``None`` when lxml
        # is absent (a slim/remote install), so the regex fallbacks remain the degraded path.
        if tree is None and is_html:
            tree = parse_html(text)
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
