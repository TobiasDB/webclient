"""URL canonicalization -- so a crawl treats the many URLs that name ONE page as one.

The same page is reachable as ``/p``, ``/p/``, ``/p?utm_source=x``, ``/p#section``,
``HTTP://Host/p`` -- crawling each is wasted fetches and duplicate results. :func:`canonical`
folds these to a single key: lowercased host, default port and fragment dropped, tracking params
removed, query sorted, an empty path normalised to ``/``. It is the dedup key, not a replacement
for the URL fetched (the original is still what we request).
"""

from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

#: query parameters that never change WHICH page you get -- analytics / ad click ids.
_TRACKING = frozenset({
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "fbclid", "gclid", "dclid", "gclsrc", "msclkid", "mc_cid", "mc_eid", "_ga", "ref", "ref_src",
})
_DEFAULT_PORTS = {"http": "80", "https": "443"}


def canonical(url: str) -> str:
    """A canonical dedup key for ``url`` (see the module docstring)."""
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    netloc = host
    if parts.port is not None and str(parts.port) != _DEFAULT_PORTS.get(scheme):
        netloc = f"{host}:{parts.port}"
    query = urlencode(sorted(
        (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k.lower() not in _TRACKING
    ))
    path = parts.path or "/"
    if len(path) > 1 and path.endswith("/"):  # /p/ == /p (but keep the root "/")
        path = path.rstrip("/")
    return urlunsplit((scheme, netloc, path, query, ""))  # fragment dropped


__all__ = ["canonical"]
