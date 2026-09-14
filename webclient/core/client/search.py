"""SearchBacking: the client's ``search`` verb -- a web search that returns
structured :class:`~webclient.models.SearchResult` hits (title / url / description).

It is a client verb like ``fetch`` (an IO op the interface bridges), and it is
built *on the interface itself*: it fetches the provider's results page with
``core.afetch`` and reads each hit with the document's own ``select`` / ``attr`` /
``text_content``. So the whole feature is one backing + one value model, with no
new transport and no core plumbing -- the extensibility test.

The default provider is DuckDuckGo's HTML endpoint; ``endpoint`` overrides it
(tests point it at a local server). The result markup is provider-specific, so
the CSS selectors live here with the endpoint.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ...errors import RETURN
from ..reference import from_url
from .models import SearchResult
from ..web_core import Backing

if TYPE_CHECKING:
    from . import WebClientCore


def _target(url: str) -> str:
    """The real destination of a result link. DuckDuckGo wraps external links in a
    redirect (``.../l/?uddg=<encoded target>``); unwrap that to the true URL. A
    direct href (any other provider, or a test fixture) passes through unchanged."""
    if "uddg=" not in url:
        return url
    from urllib.parse import parse_qs, urlparse

    q = parse_qs(urlparse(url).query)  # parse_qs already percent-decodes the value
    return q["uddg"][0] if q.get("uddg") else url


class SearchBacking(Backing):
    """The client's ``search`` verb: a web search returning structured hits."""

    provides = frozenset({"search"})
    io = frozenset({"search"})  # an IO op: the interface bridges it (dispatch)
    gate = "ok"

    #: the search provider -- DuckDuckGo's HTML endpoint. Overridable per call
    #: (``endpoint=``); the selectors below match this endpoint's result markup.
    ENDPOINT = "https://html.duckduckgo.com/html/"
    RESULT = ".result"  # one hit
    LINK = ".result__a"  # the hit's title + href
    SNIPPET = ".result__snippet"  # the hit's description

    async def search(
        self,
        core: "WebClientCore",
        query: str,
        *,
        limit: int = 10,
        endpoint: str | None = None,
    ) -> "list[SearchResult]":
        """Search ``query`` and return up to ``limit`` structured hits (title / url
        / description). An IO op -- the interface bridges it (``dispatch``)."""
        ref = from_url(endpoint or self.ENDPOINT, "get", params={"q": query})
        ref._client = core
        doc = await core.afetch(ref)
        results: list[SearchResult] = []
        # the core implements its ops (via its generated interface), so a backing
        # reads them directly and typed -- no ``dispatch("...")`` string, no cast.
        for hit in doc.select_all(self.RESULT):  # ``limit`` counts real hits, below
            if len(results) >= limit:
                break
            link = hit.select(self.LINK, error=RETURN)
            if not link.ok:  # a non-result row (ads / "no results") -- skip
                continue
            snippet = hit.select(self.SNIPPET, error=RETURN)
            results.append(
                SearchResult(
                    rank=len(results) + 1,  # 1-based rank among the real hits
                    title=(link.text_content or "").strip(),
                    url=_target(link.attr("href").url),
                    description=(snippet.text_content or "").strip(),
                )
            )
        return results


__all__ = ["SearchBacking"]
