"""Value models the client backings produce -- currently the search hit from
``SearchBacking`` (``client.search(query)``)."""

from __future__ import annotations

from pydantic import BaseModel


class SearchResult(BaseModel):
    """One search hit: ``title`` / ``url`` / ``description`` as the provider gave
    them, plus ``rank`` (1-based position on the results page)."""

    rank: int = 0
    title: str = ""
    url: str = ""
    description: str = ""


__all__ = ["SearchResult"]
