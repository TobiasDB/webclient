"""Task verbs: thin, LLM/agent-friendly functions over the tool registry.

Each is one registered tool (:mod:`webclient.tools`) called with keyword arguments and
an optional ``client`` -- the shape a quick script or an LLM tool wants (markdown, text,
links, a skeleton, rows). Without a client they use the process-local ``default_client()``
(share a ``with WebClient() as wc`` for lifecycle control).
"""

from __future__ import annotations

from typing import Any

from ..interface import WebClient
from ..tools import dispatch


def fetch_markdown(url: str, *, client: WebClient | None = None, **kw: Any) -> str:
    """Fetch ``url`` and return its content rendered as markdown."""
    return str(dispatch("fetch_markdown", {"url": url, **kw}, client))


def fetch_text(
    url: str,
    *,
    main_content_only: bool = True,
    client: WebClient | None = None,
    **kw: Any,
) -> str:
    """Fetch ``url`` and return its readable text (nav/chrome stripped by
    default)."""
    return str(dispatch("fetch_text", {"url": url, "main_content_only": main_content_only, **kw}, client))


def links(url: str, *, client: WebClient | None = None, **kw: Any) -> list[str]:
    """Fetch ``url`` and return its outbound link URLs (absolute)."""
    return list(dispatch("links", {"url": url, **kw}, client))


def page_skeleton(
    url: str, *, browser: Any = False, client: WebClient | None = None, **kw: Any
) -> str:
    """Fetch ``url`` and return its token-lean DOM skeleton -- an HTML-tag outline
    an LLM reads to write CSS selectors (bloat removed, leaf text hinted, every
    sibling shown). Pass ``browser="auto"`` for a JS/SPA page: the skeleton then
    marks client-injected nodes ``[xhr]``/``[js]`` and lists the data APIs. ``**kw``
    forwards ``max_lines`` / ``collapse`` / ``drop_chrome``."""
    return str(dispatch("skeleton", {"url": url, "browser": browser, **kw}, client))


def extract(
    url: str,
    result: str,
    fields: dict[str, str],
    *,
    limit: int | None = None,
    client: WebClient | None = None,
    **kw: Any,
) -> list[dict[str, Any]]:
    """Extract rows from ``url``: ``result`` is the CSS selector for each row
    element, and ``fields`` maps output keys to a CSS selector whose text is that
    column's value. Returns a list of plain dicts."""
    return list(dispatch("extract", {"url": url, "result": result, "fields": fields, "limit": limit, **kw}, client))


__all__ = ["fetch_markdown", "fetch_text", "links", "page_skeleton", "extract"]
