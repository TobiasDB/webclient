"""``Request`` -- the input to the fetch layer.

A fully-formed request spec: a complete URL plus how to send it. The fetch layer does not
build URLs (join relative links, add query params) -- that is a higher concern; it takes a URL
and performs it. Keeping ``Request`` a plain value means a fetcher can be driven in isolation:
``await HttpFetcher().fetch(Request(url="https://example.com"))``.
"""

from __future__ import annotations

from pydantic import BaseModel


class Request(BaseModel):
    """One HTTP request to perform. ``url`` is complete (scheme + host + path + query)."""

    url: str
    method: str = "GET"
    headers: dict[str, str] = {}
    cookies: dict[str, str] = {}
    body: bytes | None = None
    timeout: float = 30.0
    follow_redirects: bool = True


__all__ = ["Request"]
