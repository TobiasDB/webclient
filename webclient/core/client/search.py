"""SearchBacking: the client's ``search`` verb -- a web search that returns
structured :class:`~webclient.models.SearchResult` hits (title / url / description).

It is a client verb like ``fetch`` (an IO op the interface bridges), and it is
built *on the interface itself*: it fetches a provider's results page with
``core.afetch`` and reads each hit with the document's own ``select`` / ``attr`` /
``text_content``. So the whole feature is one backing + a couple of value models,
with no new transport.

Robustness: search engines block a library/empty ``User-Agent`` (this is why the
old single-provider search silently failed), so every request carries a realistic
browser UA. It then tries a chain of providers, falling back to the next when one
is blocked or returns nothing; ``browser=True`` renders the results page in a real
browser (a real fingerprint) as the strongest anti-bot bypass. ``endpoint``
overrides the chain with a single provider (tests point it at a local server).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import quote_plus

from ...errors import RETURN
from ..reference import from_url
from .models import SearchResult
from ..web_core import Backing

if TYPE_CHECKING:
    from . import WebClient
    from ..document import Document

#: a realistic desktop-Chrome UA -- engines refuse a library/empty one.
_SEARCH_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)


def _target(url: str) -> str:
    """The real destination of a result link. DuckDuckGo wraps external links in a
    redirect (``.../l/?uddg=<encoded target>``); unwrap that to the true URL. A
    direct href (any other provider, or a test fixture) passes through unchanged."""
    if "uddg=" not in url:
        return url
    from urllib.parse import parse_qs, urlparse

    q = parse_qs(urlparse(url).query)  # parse_qs already percent-decodes the value
    return q["uddg"][0] if q.get("uddg") else url


@dataclass(frozen=True)
class _Provider:
    """A results-page shape. ``mode="container"``: each ``row`` element holds a
    ``link`` anchor + a ``snippet``. ``mode="flat"``: ``row`` selects the anchors
    directly and ``snippet`` a parallel list (paired by index)."""

    name: str
    url: str  # a "...q={q}" template; ``{q}`` is url-encoded
    row: str
    link: str
    snippet: str
    mode: str = "container"


#: the fallback chain -- tried in order; the first that yields hits wins. DuckDuckGo's
#: lite endpoint is cleanest/most tolerant, its html endpoint the fallback.
_PROVIDERS: tuple[_Provider, ...] = (
    _Provider("ddg-lite", "https://lite.duckduckgo.com/lite/?q={q}",
              "a.result-link", "", ".result-snippet", "flat"),
    _Provider("ddg-html", "https://html.duckduckgo.com/html/?q={q}",
              ".result", ".result__a", ".result__snippet", "container"),
)


def _is_junk(url: str) -> bool:
    """A non-organic result: an ad / internal link still on the engine's own domain
    after unwrapping (DDG's ``y.js`` ad rows, help pages, empty hrefs)."""
    from urllib.parse import urlparse

    host = (urlparse(url).hostname or "").lower()
    return not url or host.endswith("duckduckgo.com")


def _parse(doc: "Document", prov: _Provider, limit: int) -> list[SearchResult]:
    """Read up to ``limit`` organic hits from a results page per the provider's shape
    (ads and engine-internal links skipped)."""
    results: list[SearchResult] = []
    if prov.mode == "flat":
        anchors = doc.select_all(prov.row)
        snippets = list(doc.select_all(prov.snippet)) if prov.snippet else []
        for i, a in enumerate(anchors):
            if len(results) >= limit:
                break
            url = _target(a.attr("href").url)
            title = (a.text_content or "").strip()
            if _is_junk(url) or not title:
                continue
            snip = (snippets[i].text_content or "").strip() if i < len(snippets) else ""
            results.append(
                SearchResult(rank=len(results) + 1, title=title, url=url, description=snip)
            )
        return results
    for row in doc.select_all(prov.row):  # container
        if len(results) >= limit:
            break
        link = row.select(prov.link, error=RETURN)
        if not link.ok:  # a non-result row (ads / "no results") -- skip
            continue
        url = _target(link.attr("href").url)
        if _is_junk(url):
            continue
        snippet = row.select(prov.snippet, error=RETURN) if prov.snippet else None
        results.append(
            SearchResult(
                rank=len(results) + 1,
                title=(link.text_content or "").strip(),
                url=url,
                description=(snippet.text_content if snippet and snippet.ok else "") or "",
            )
        )
    return results


class SearchBacking(Backing):
    """The client's ``search`` verb: a web search returning structured hits."""

    provides = frozenset({"search"})
    io = frozenset({"search"})  # an IO op: the interface bridges it (dispatch)
    gate = "ok"

    async def search(
        self,
        core: "WebClient",
        query: str,
        *,
        limit: int = 10,
        endpoint: str | None = None,
        browser: Any = False,
        optional: bool = False,
        error: Any = None,
    ) -> "list[SearchResult]":
        """Search ``query`` -> up to ``limit`` structured hits (title / url /
        description). Sends a browser ``User-Agent`` and tries a provider chain,
        falling back when one is blocked/empty; ``browser=True`` renders the results
        page in a real browser (strongest anti-bot bypass). ``endpoint`` overrides
        the chain with a single provider. LOUD by default: if every provider fails,
        it raises a ``WebException`` -- ``optional=True`` / ``error=RETURN`` returns
        ``[]`` instead."""
        from ...errors import WebException, error_for, lenient

        lenient_ = lenient(optional, error)
        if endpoint is not None:  # a single explicit provider (ddg-html-shaped)
            plan = [(
                _Provider("custom", endpoint, ".result", ".result__a", ".result__snippet"),
                from_url(endpoint, params={"q": query}, headers={"User-Agent": _SEARCH_UA}),
            )]
        else:
            plan = [
                (p, from_url(p.url.format(q=quote_plus(query)), headers={"User-Agent": _SEARCH_UA}))
                for p in _PROVIDERS
            ]

        last: Exception | None = None
        for prov, ref in plan:
            ref._client = core
            try:
                doc = await core.afetch(ref, optional=True, browser=browser)
            except WebException as exc:  # transport failure -> try the next provider
                last = exc
                continue
            if browser and getattr(doc, "_page", None) is not None:
                await core._arelease(doc)  # capture content, drop the live page
            if not doc.ok:
                last = WebException(doc.error or error_for(doc.status_code), document=doc)
                continue
            hits = _parse(doc, prov, limit)
            if hits:
                return hits  # this provider worked
            # ok but empty -> likely a soft block / no results; fall through to next

        if lenient_:
            return []
        raise last or WebException(
            error_for(0, "search: every provider returned no results (possibly blocked)")
        )


__all__ = ["SearchBacking"]
