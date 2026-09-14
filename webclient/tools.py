"""Task verbs: thin, LLM/agent-friendly functions over the surface.

These hide the lazy-plan machinery and return ready-to-use values -- markdown,
plain text, links, or rows of extracted fields -- the shape an LLM tool or a
quick script wants (Mode 3 in ``docs/llm-usability.md``; cf. Firecrawl/Tavily).
Each takes an optional ``client``; without one it uses the process-local
``default_client()`` (share a ``with WebClient() as wc`` for lifecycle control).
"""

from __future__ import annotations

from typing import Any

from .expr import doc
from .surfaces import WebClient, default_client


def _client(client: WebClient | None) -> WebClient:
    return client if client is not None else default_client()


def fetch_markdown(url: str, *, client: WebClient | None = None, **kw: Any) -> str:
    """Fetch ``url`` and return its content rendered as markdown."""
    return _client(client).fetch(url, **kw).collect().render("markdown")


def fetch_text(
    url: str,
    *,
    main_content_only: bool = True,
    client: WebClient | None = None,
    **kw: Any,
) -> str:
    """Fetch ``url`` and return its readable text (nav/chrome stripped by
    default)."""
    page = _client(client).fetch(url, **kw).collect()
    return page.render("text", main_content_only=main_content_only)


def links(url: str, *, client: WebClient | None = None, **kw: Any) -> list[str]:
    """Fetch ``url`` and return its outbound link URLs (absolute)."""
    page = _client(client).fetch(url, **kw).collect()
    return [r.url for r in page.render("links")]


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
    exprs = {name: doc.select(sel).text_content for name, sel in fields.items()}
    rows = _client(client).fetch(url, **kw).select_all(result)
    if limit is not None:
        rows = rows.limit(limit)
    return rows.extract(**exprs).collect().project()


__all__ = ["fetch_markdown", "fetch_text", "links", "extract"]
