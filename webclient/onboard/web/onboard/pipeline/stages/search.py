"""Stage 1 -- search: the brief's term through the search engine, every hit SCORED by the brief's
domain / path hints. Deterministic: no model call."""

from __future__ import annotations

from urllib.parse import urlparse

from ...search import as_hits
from ..ask import Context
from ..state import Hit, Onboarding, SearchResult


def score_url(
    url: str, domain: "list[str]", path: "list[str]"
) -> "tuple[float, list[str], list[str]]":
    """``(score, matched domain hints, matched path hints)``: a domain fragment in the host counts
    2, a path fragment in the path counts 1 (fragments are case-insensitive substrings)."""
    parts = urlparse(url)
    host, where = (parts.hostname or "").lower(), parts.path.lower()
    d = [h for h in domain if h and h.lower() in host]
    p = [h for h in path if h and h.lower() in where]
    return 2.0 * len(d) + 1.0 * len(p), d, p


async def run(state: Onboarding, ctx: Context) -> SearchResult:
    spec = state.brief.search
    term = spec.term or state.brief.goal
    found = as_hits(await ctx.search(term))[: spec.k]
    hits: list[Hit] = []
    for h in found:
        score, d, p = score_url(h.url, spec.domain, spec.path)
        hits.append(Hit(url=h.url, title=h.title, snippet=h.snippet, score=score, domain=d, path=p))
    hits.sort(key=lambda h: -h.score)
    if not hits:
        state.stopped = "search: no results"
    return SearchResult(term=term, hits=hits)
