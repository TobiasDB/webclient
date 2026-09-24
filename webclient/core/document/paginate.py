"""Pagination: walk a paginated dataset into a ``Collection`` of same-structure pages.

Pagination is the complement of crawl: crawl walks across STRUCTURE (different pages), paginate
walks across CONTENT within one structure -- an ordered series of pages that share a shape, so one
extraction authored on any page is valid on all. ``doc.paginate(...)`` yields the pages as a
``Collection[Document]``; the rest of the chain (``select_all(record).extract(...).project()``)
then runs across every page via the executor's flat-map, so ``.collect()`` returns the WHOLE
dataset's rows -- not page 1 only.

``paginate`` is a BOUND op (hand-written on ``Document`` as ``apaginate``, like ``extract`` -- the
executor passes its ``stop``/``key`` sub-plans UNEVALUATED and this walk evaluates them per page):
that is what lets a caller stop on a semantic predicate the DSL expresses rather than only a literal
cutoff. This module owns the walk (:func:`walk`), the advance (:func:`_next_ref`), and ``next_link``
(the one backing op left here -- "where is the next page"). ``Document.apaginate`` is the thin bound
method that runs :func:`walk` and wraps the pages in a ``Collection``.

The walk is HTTP + sequential. Advance strategies: ``by="auto"`` (default) resolves the advance from
the page's detected ``pagination`` hint (param source -> ``by="param"``, else ``by="link"``), so a
bare ``paginate()`` works; ``by="link"`` follows a ``rel="next"`` link discovered on each page;
``by="param"`` increments a page/offset query parameter; ``by="cursor"`` reads a keyset/cursor token
off each page (a selector + attribute, or a JSON path) and carries it in the next request -- so a
cursor API paginates too. It is bounded by ``max_pages`` and guarded against
the common out-of-range CLAMP (``?page=999`` re-serving an earlier page) by a per-page key -- a
content fingerprint by default, or a semantic ``key=<Expr>`` -- so a repeat stops the walk rather than
looping. It can stop EARLY on ``max_rows`` (enough records collected), a recency cutoff
(``until``/``until_before`` -- literal selector + value), or a general ``stop=<Expr>`` predicate
(truthy against a page -> that page is the last), so a long dataset isn't walked whole for a few rows.

Interacted (load-more / infinite scroll) pagers and the semantics flags (ordered/filtered/live) are
later phases.
"""

from __future__ import annotations

import asyncio
import re
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import urljoin

from ...dom import clean_href
from ..web_core import Backing
from .html import tree

if TYPE_CHECKING:
    from ..reference import Reference
    from . import Document

#: a hard ceiling so a pathological advance rule can never fetch without bound.
_MAX_PAGES_CAP = 200

#: an RFC 8288 ``Link:`` header entry pointing at the next page (``<url>; rel="next"``), as GitHub
#: and other APIs paginate. The ``rel`` may sit after other link-params (``; title=…; rel=next``).
_LINK_NEXT = re.compile(r'<([^>]+)>\s*;\s*[^,]*\brel\s*=\s*"?next"?', re.I)


def _link_header_next(link_header: str) -> "str | None":
    """The next-page URL from an HTTP ``Link:`` header (its ``rel="next"`` entry), or ``None``."""
    m = _LINK_NEXT.search(link_header)
    return m.group(1).strip() if m else None


def _page_fingerprint(doc: "Document") -> int:
    """A cheap content fingerprint of a page, to detect an out-of-range CLAMP (a server that
    re-serves an earlier page for an over-range index) -- a repeated fingerprint stops the walk."""
    return hash(doc.content or b"")


def _ref_of(doc: "Document") -> "Reference":
    """The reference that produced ``doc`` (for deriving the next page's URL), rebuilt from its
    URL when the producing reference wasn't retained."""
    from ..reference import from_url

    return doc._ref if doc._ref is not None else from_url(doc.final_url or doc.url)


def _read_one(doc: "Document", selector: str, attr: str) -> "str | None":
    """The ``attr`` of the FIRST element matching ``selector`` on ``doc`` -- the ``text`` pseudo-attr
    for its text, or a real attribute name (``value``, ``data-cursor``, …); ``None`` when nothing
    matches or the value is empty. Goes through the document's own select/attr ops, so it reads a
    CSS selector on an HTML page and a dotted JSON path on an API page alike (how a cursor token is
    read off each page)."""
    el = doc.select(selector, optional=True)
    if not el.ok:
        return None
    val = el.attr(attr, optional=True)
    return val if isinstance(val, str) and val else None


def _read_all(doc: "Document", selector: str, attr: str) -> "list[str]":
    """Every match's ``attr`` on ``doc`` (the ordering-field values across a page), empties dropped."""
    out: list[str] = []
    for el in doc.select_all(selector):
        val = el.attr(attr, optional=True)
        if isinstance(val, str) and val:
            out.append(val)
    return out


def _row_count(doc: "Document", records: str) -> int:
    """How many records ``records`` matches on ``doc`` (0 when no record selector is given)."""
    return sum(1 for _ in doc.select_all(records)) if records else 0


def _past_cutoff(doc: "Document", until: str, before: str) -> bool:
    """Whether ``doc`` already reaches records at/older than the ``before`` cutoff: True once the
    OLDEST ``until`` value on the page sorts below ``before`` (a lexical compare -- exact for ISO
    dates and zero-padded ids). The triggering page is still kept; the walk just stops after it,
    since every later page is older still."""
    vals = _read_all(doc, until, "text")
    return bool(vals) and min(vals) < before


class PaginateBacking(Backing):
    """The ``next_link`` op: "where is the next page of this dataset?" -- read once per page and
    followed by ``by="link"`` pagination. (The walk itself is the bound op ``Document.apaginate``,
    which lives on the core so it can evaluate ``stop``/``key`` Exprs per page; see :func:`walk`.)"""

    provides = frozenset({"next_link"})
    gate = "ok"

    def applies(self, core: "Document") -> bool:
        """In play for any resolved document -- pagination starts from a fetched page one."""
        return True

    def next_link(self, core: "Document") -> "Reference":
        """The reference to the NEXT page of this dataset, or an empty (not-ok) reference when
        there is none. Reads the HTTP ``Link: <url>; rel="next"`` header first (so a JSON/API
        listing paginates), then an HTML ``a[rel="next"]`` / ``link[rel="next"]`` -- either way
        resolved against the page's URL. The one place "where's the next page" is answered."""
        from ..reference import from_url

        base = core.final_url or core.url
        headers = {k.lower(): v for k, v in core.response_headers.items()}
        raw = headers.get("link")
        if raw and (url := _link_header_next(raw)):
            return from_url(urljoin(base, url))
        if core.kind in ("html", "xml"):
            root = tree(core)
            if root is not None:
                for node in root.cssselect('a[rel="next"], link[rel="next"]'):
                    href = clean_href(node.get("href"))
                    if href:
                        return from_url(urljoin(base, href))
        return from_url("")  # empty -> ok is False -> "no next page"


def _next_ref(
    current: "Document", *, by: str, name: str, size: int, start: int, step: int,
    index: int, cursor: str, cursor_attr: str,
) -> "Reference | None":
    """The reference for the page AFTER ``current`` (the ``index``-th already collected), or
    ``None`` to stop. ``by="link"`` reads the next link off ``current`` (Link header / rel=next);
    ``by="param"`` computes the next ``?name=`` value (a page number, or an offset when ``size`` is
    set); ``by="cursor"`` reads a keyset token off ``current`` (the ``cursor`` selector's
    ``cursor_attr``) and carries it in ``?name=`` -- no token means no next page."""
    if by == "cursor":
        token = _read_one(current, cursor, cursor_attr)
        if not token:
            return None  # the page carries no next-cursor -> the last page
        return cast("Reference", _ref_of(current).dispatch("with_params", **{name: token}))
    if by == "param":
        value = (start + index * size) if size else (start + index * step)
        return cast("Reference", _ref_of(current).dispatch("with_params", **{name: str(value)}))
    nxt = current.next_link()  # by == "link": Link header or an HTML rel=next
    return nxt if nxt.ok else None


def _key_of(value: Any) -> Any:
    """A hashable clamp-key from an evaluated ``key`` Expr value: a ``Field`` unwrapped to its
    scalar, a list/collection frozen to a tuple, anything else as-is."""
    from ...query.collection import Collection, Field

    if isinstance(value, Field):
        value = value.get()
    if isinstance(value, (list, tuple, Collection)):
        return tuple(str(v) for v in value)
    return value


async def _page_key(doc: "Document", key: Any, client: Any) -> Any:
    """The clamp-key for ``doc``: a semantic ``key`` Expr evaluated against the page (so pages that
    differ only by chrome/timestamps but repeat their records are caught), else a content hash."""
    if key is None:
        return _page_fingerprint(doc)
    from ...query.executor import aevaluate

    return _key_of(await aevaluate(key, doc, client=client))


async def _stop_here(stop: Any, doc: "Document", client: Any) -> bool:
    """Whether the ``stop`` predicate Expr is truthy against ``doc`` (this page is then the last)."""
    from ...query.executor import aevaluate, truthy

    return truthy(await aevaluate(stop, doc, client=client))


def _auto_advance(doc: "Document", name: str) -> "tuple[str, str]":
    """Resolve ``by="auto"`` from the page's detected ``pagination`` flag (its :class:`PaginationHint`):
    a computed source (``kind="param"`` with a known param) -> ``("param", <param>)``; a discovered
    link, a numbered strip, or no hint at all -> ``("link", name)`` -- the safe default. So a bare
    ``doc.paginate()`` walks the source the way the detector read it, with ``by="link"`` as the
    fallback that follows ``rel=next`` / the HTTP Link header."""
    hint = doc.pagination().value
    if hint is not None and getattr(hint, "kind", "") == "param" and getattr(hint, "name", ""):
        return "param", hint.name
    return "link", name


#: how many computed pages to fetch at once when the total is known (a bounded fan-out).
_PARALLEL = 8


async def _gather_bounded(refs: "list[Reference]", client: Any, limit: int) -> "list[Document]":
    """Fetch ``refs`` CONCURRENTLY, at most ``limit`` in flight, results in input order."""
    sem = asyncio.Semaphore(max(1, limit))

    async def _one(ref: "Reference") -> "Document":
        async with sem:
            return cast("Document", await client.afetch(ref, optional=True))

    return list(await asyncio.gather(*(_one(ref) for ref in refs)))


async def _parallel_pages(
    doc: "Document", *, name: str, start: int, step: int, size: int, total: int, client: Any
) -> "list[Document]":
    """The computed fast-path: for ``by="param"`` with a KNOWN total, the next page is a pure
    function of the index, so build every page's reference up front and fetch them CONCURRENTLY
    (bounded), page one first. Stops early at the first empty/not-ok page or a clamped repeat (a
    wrong total), so the result never runs past the real end."""
    refs = [
        cast("Reference", _ref_of(doc).dispatch(
            "with_params", **{name: str((start + index * size) if size else (start + index * step))}
        ))
        for index in range(1, total)  # page one is ``doc`` (index 0)
    ]
    pages: list[Document] = [doc]
    seen = {_page_fingerprint(doc)}
    for page in await _gather_bounded(refs, client, _PARALLEL):
        if not (page.ok and page.content):
            break
        fp = _page_fingerprint(page)
        if fp in seen:
            break
        seen.add(fp)
        pages.append(page)
    return pages


async def _interact_pages(
    doc: "Document", *, action: Any, records: str, max_pages: int, client: Any
) -> "list[Document]":
    """Interacted (append / exhaust-then-extract) pagination for a JS pager: drive the ``action``
    -- a click on a "load more" button, or a scroll -- against the HELD live page until the record
    count stops growing (exhausted), then emit the ONE fully-loaded page. Each interaction refreshes
    the page's captured content, so the final ``select_all`` over that page sees every loaded record.
    ``records`` (the record selector) measures progress; without it, the content length does."""
    from ...query.executor import aevaluate

    bound = max(1, min(max_pages, _MAX_PAGES_CAP))
    # each action runs in its own plan scope, which would RELEASE the held live page (a click
    # returns the page); keep it alive across the whole interaction so the caller keeps their page.
    prev_keep = getattr(doc, "_keep_alive", False)
    doc._keep_alive = True
    try:
        prev = _row_count(doc, records) if records else len(doc.content or b"")
        for _ in range(bound):
            try:
                await aevaluate(action, doc, client=client)  # load more / scroll -> refreshes doc.content
            except Exception:  # noqa: BLE001 - a gone "load more" button / failed action = exhausted
                break
            count = _row_count(doc, records) if records else len(doc.content or b"")
            if count <= prev:
                break  # nothing new loaded -> the list is exhausted
            prev = count
    finally:
        doc._keep_alive = prev_keep
    return [doc]


async def walk(
    doc: "Document",
    *,
    by: str = "auto",
    max_pages: int = 20,
    max_rows: int = 0,
    name: str = "page",
    start: int = 1,
    step: int = 1,
    size: int = 0,
    cursor: str = "",
    cursor_attr: str = "text",
    records: str = "",
    until: str = "",
    until_before: str = "",
    total_pages: int = 0,
    action: Any = None,
    stop: Any = None,
    key: Any = None,
    client: Any = None,
) -> "list[Document]":
    """Walk ``doc``'s dataset into a flat list of pages (page one first). Fetches each next page
    (see :func:`_next_ref`) until there is no next page, a page comes back empty/not-ok, a page
    REPEATS an earlier one (by ``key``, else a content fingerprint -- an out-of-range clamp), or a
    stop fires: ``max_pages``, ``max_rows`` (with ``records``), the ``until``/``until_before``
    recency cutoff, or the ``stop`` predicate. ``stop``/``key`` are Exprs (or ``None``) evaluated
    per page -- the bound-op capability. The engine ``client`` fetches subsequent pages.

    COMPUTED FAST-PATH: when the advance is ``param`` and the total page count is known
    (``total_pages``, or the hint's when ``by="auto"``) and no per-page semantic stop is in play,
    every page reference is a pure function of its index, so the pages are fetched CONCURRENTLY
    (bounded) instead of one-at-a-time."""
    client = client if client is not None else doc._client
    if by == "action":  # a JS pager -- drive the action on the held live page (append/exhaust)
        return await _interact_pages(doc, action=action, records=records, max_pages=max_pages, client=client) if action is not None else [doc]
    if by == "auto":
        by, name = _auto_advance(doc, name)  # pick the advance from the detected pagination hint
        if not total_pages:  # ... and its known total, for the parallel fast-path (hint is cached)
            hint = doc.pagination().value
            total_pages = int(getattr(hint, "total_pages", 0) or 0) if hint is not None else 0
    bound = max(1, min(max_pages, _MAX_PAGES_CAP))
    if by == "param" and total_pages > 1 and not (until or stop or key or max_rows):
        return await _parallel_pages(
            doc, name=name, start=start, step=step, size=size,
            total=min(total_pages, bound), client=client,
        )
    pages: list[Document] = [doc]
    seen = {await _page_key(doc, key, client)}
    rows = _row_count(doc, records)
    current = doc
    while len(pages) < bound:
        if max_rows and rows >= max_rows:
            break  # collected enough rows -> no need to fetch further pages
        if until and until_before and _past_cutoff(current, until, until_before):
            break  # this page already reaches the cutoff; every later page is older -> stop
        if stop is not None and await _stop_here(stop, current, client):
            break  # the predicate says this page is the last
        nxt = _next_ref(
            current, by=by, name=name, size=size, start=start, step=step,
            index=len(pages), cursor=cursor, cursor_attr=cursor_attr,
        )
        if nxt is None:
            break
        page = await client.afetch(nxt, optional=True)
        if not (page.ok and page.content):
            break  # ran off the end (a 404 / empty page)
        k = await _page_key(page, key, client)
        if k in seen:
            break  # the same page again -> an out-of-range clamp; stop rather than loop
        seen.add(k)
        pages.append(page)
        rows += _row_count(page, records)
        current = page
    return pages


__all__ = ["PaginateBacking", "walk"]
