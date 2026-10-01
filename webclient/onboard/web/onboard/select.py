"""Select -- the CHEAP narrowing stage between the crawl and the (expensive) evaluate.

ONE model call over ALL the crawled pages, and the model sees only METADATA -- ``{url, title,
kind, flags}`` per page, clipped to a budget -- never a skeleton or page text. It ranks the pages
worth evaluating into ``must`` / ``should`` / ``could`` tiers (with a kind and a one-line reason),
so the evaluate stage that DOES read a skeleton runs on a short, ordered list and can stop early.

Before the model sees anything, the deterministic gates apply: a brief-forbidden host
(``ignore``), API documentation, and a page whose signals say "wall / no dataset" are dropped --
those are ground truth, not a judgement call. A seeded DATA document (a JSON/XML feed the caller
pointed us at) is forced in as a ``must``: it IS the dataset, and a metadata-only filter routinely
drops a raw feed. Without a model the ranking is the deterministic score. FAILS OPEN: if the model
picks nothing (variance, a parse miss), the top few by score are kept, so the run still evaluates a
real source instead of dying at "no candidates".
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

import json
from collections.abc import Sequence

from web.fetch import WebException, emit
from web.parse import Document
from web.resolve import Flag, flags

from .evaluate import TIER_RANK, download_targets, field_bonus, ignored, is_docs, score
from .llm import Llm, ReasonEvent, parse_json
from .models import Candidate, LocateBrief
from .patterns import brief_hints
from .prompts import MAX_PAGES_CHARS, clip, render_prompt

#: how many top-scored pages to keep when the model's pick is unusable (fail-open).
_FAIL_OPEN_KEEP = 3


def _rank(doc: Document, brief: LocateBrief) -> "tuple[float, dict[str, Flag]] | None":
    """The deterministic gate + rank for one crawled page: ``None`` when it can never be a source
    (a forbidden host, API docs, a wall / no dataset), else its score (+ the schema-match tiebreak,
    + the download-listing bonus for a download brief) and the page's flags."""
    if ignored(
        doc.url, brief.ignore
    ):  # a brief-forbidden host never wins, even as the only survivor
        emit(
            ReasonEvent(
                stage="select", subject=doc.url, text="skipped — matches the brief's ignore list"
            )
        )
        return None
    if is_docs(doc.url):
        return None
    by = {f.name: f for f in flags(doc)}
    if _single_record(doc, by):  # one article / release / event: a leaf, never the listing
        emit(
            ReasonEvent(
                stage="select",
                subject=doc.url,
                text="skipped — a single record (a detail page), not the listing",
            )
        )
        return None
    s = score(doc, by)
    if brief.download:  # a DOWNLOAD brief: a page that LISTS the target files IS the source
        targets = download_targets(doc)
        if targets:
            s = max(s, 5.0 + min(len(targets), 20) * 0.1)
    if s <= 0.0:
        return None
    return s + field_bonus(doc, brief.fields), by


#: a URL path that names ONE record: a detail segment, a dated slug, a long hyphenated slug, a
#: trailing numeric id.
_DETAIL_PATH = re.compile(
    r"/(detail|details|article|articles|release|press-release|event|events|news|story|post|blog)s?/"
    r"(?:\d{4}/\d{1,2}/(?:\d{1,2}/)?)?[^/]*[a-z][^/]*-[^/]*-[^/]*-[^/]+/?$"
    r"|/\d{4}/\d{2}/\d{2}/[^/]+/?$|[-/]\d{3,}/?$|/detail/\d+/",
    re.I,
)


def _single_record(doc: Document, by: "dict[str, Flag]") -> bool:
    """A page that IS one record: a detail-shaped URL on a page with no paginated / real record
    region (the only repeating things are nav, tags or related links)."""
    if doc.kind != "html" or "paginated" in by:
        return False
    path = urlparse(doc.url).path.rstrip("/")
    if not _DETAIL_PATH.search(path + "/"):
        return False
    # the paragraphs / links of ONE article register as a "record region" too (item selector `p`,
    # `a`, `li`): only a CLASSED repeating item of real size says this page is a listing
    regions = [r for r in doc.records(top_k=3) if any(c in r.item_selector for c in ".#[")]
    return not regions or regions[0].count < 6


def registrable(url: str) -> str:
    """The registrable domain of a URL (``investors.10xgenomics.com`` -> ``10xgenomics.com``) --
    good enough for same-company checks without a public-suffix list (a two-part public suffix
    such as ``co.uk`` keeps three labels)."""
    host = (urlparse(url).hostname or "").lower()
    parts = host.split(".")
    two_part = len(parts) >= 3 and parts[-2] in ("co", "com", "org", "net", "ac", "gov")
    if two_part and len(parts[-1]) == 2:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def _page_row(doc: Document, by: "dict[str, Flag]") -> "dict[str, object]":
    """What the model sees for one page: url, title, kind, and the flag NAMES that fired."""
    meta = doc.metadata()
    return {
        "url": doc.url,
        "title": (meta.title or "").strip(),
        "kind": doc.kind,
        "flags": sorted(n for n, f in by.items() if f.present),
    }


async def select_candidates(
    docs: "Sequence[Document]",
    brief: LocateBrief,
    *,
    llm: "Llm | None",
    seed_urls: "Sequence[str]" = (),
    entity: str = "",
    entity_domains: "Sequence[str]" = (),
) -> "list[Candidate]":
    """Rank the crawled pages into must / should / could candidates (see the module docstring).
    Deterministic gates first; then one metadata-only model call (or the score order without a
    model); a seeded data document forced in; fail-open on an unusable pick. Best tier first."""
    ranked: list[tuple[Document, float, dict[str, Flag]]] = []
    allowed = {d.lower() for d in entity_domains if d}
    for doc in docs:
        if allowed and registrable(doc.url) not in allowed:  # not the entity's own site
            emit(
                ReasonEvent(
                    stage="select",
                    subject=doc.url,
                    text=f"skipped — not {entity or 'the entity'}'s host (its verified domains: "
                    + ", ".join(sorted(allowed))
                    + ")",
                )
            )
            continue
        got = _rank(doc, brief)
        if got is not None:
            ranked.append((doc, got[0], got[1]))
    if not ranked:
        return []
    ranked.sort(key=lambda t: -t[1])
    if llm is None:  # no model -> the deterministic order is the ranking
        plain = [Candidate(url=d.url, tier="could", note=f"score {s:.1f}") for d, s, _ in ranked]
        for c in plain:
            emit(ReasonEvent(stage="select", subject=c.url, text=f"[{c.tier}] {c.note}"))
        return plain
    pages = [_page_row(d, by) for d, _, by in ranked]
    scope = (
        f"The dataset belongs to '{entity}': pick only pages HOSTED BY {entity} itself (its own "
        f"site / investor-relations host) -- a page about {entity} on someone else's domain is not "
        "its data, never pick it."
        if entity
        else ""
    )
    prompt = render_prompt(
        "select_candidates",
        description=brief.goal or "the target dataset",
        fields_line=brief_hints(brief),
        pages_json=clip(json.dumps(pages, indent=0), MAX_PAGES_CHARS, "pages list", kind="json"),
        scope=scope,
    )
    try:
        data = parse_json(await llm.complete(prompt))
    except WebException as exc:  # a model outage never sinks the run -- fall back to the score
        emit(ReasonEvent(stage="select", text=f"model error, ranking by signals: {exc}"))
        data = None
    known = {d.url for d, _, _ in ranked}
    out: list[Candidate] = []
    for row in data if isinstance(data, list) else []:
        if not isinstance(row, dict):
            continue
        url = row.get("url")
        if not isinstance(url, str) or url not in known or url in {c.url for c in out}:
            continue
        tier = row.get("tier")
        out.append(
            Candidate(
                url=url,
                kind=str(row.get("kind") or "page"),
                tier=tier if isinstance(tier, str) and tier in TIER_RANK else "could",
                note=str(row.get("reason") or row.get("note") or ""),
            )
        )
    picked = {c.url for c in out}
    # a SEEDED data document (a JSON/XML feed the caller pointed us at) IS the dataset -- it holds
    # the records, it doesn't lead to them -- and a metadata-only filter routinely drops a raw feed.
    # Restricted to SEEDS on purpose: a site can expose many feeds; forcing every one would flood.
    seeds = set(seed_urls)
    for d, _, _ in ranked:
        if d.url not in picked and d.kind in ("json", "xml") and d.url in seeds:
            out.append(
                Candidate(
                    url=d.url,
                    kind="api",
                    tier="must",
                    note="a seeded data document — the dataset itself",
                )
            )
            picked.add(d.url)
    if not out:  # FAIL OPEN: the model picked nothing usable -> keep the top few by score
        out = [
            Candidate(
                url=d.url,
                tier="could",
                note=f"fail-open: the model picked nothing — kept by score {s:.1f}",
            )
            for d, s, _ in ranked[:_FAIL_OPEN_KEEP]
        ]
    out.sort(key=lambda c: TIER_RANK.get(c.tier, 3))
    for c in out:
        emit(ReasonEvent(stage="select", subject=c.url, text=f"[{c.tier}] {c.note}"))
    return out


__all__ = ["select_candidates"]
