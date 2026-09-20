"""PaginateBacking: walk a paginated dataset into a ``Collection`` of same-structure pages.

Pagination is the complement of crawl: crawl walks across STRUCTURE (different pages), paginate
walks across CONTENT within one structure -- an ordered series of pages that share a shape, so one
extraction authored on any page is valid on all. ``doc.paginate(...)`` yields the pages as a
``Collection[Document]``; the rest of the chain (``select_all(record).extract(...).project()``)
then runs across every page via the executor's flat-map, so ``.collect()`` returns the WHOLE
dataset's rows -- not page 1 only.

This is the minimal, HTTP, sequential core (the plan's Phase 3, executor-internal). Advance
strategies: ``by="link"`` follows a ``rel="next"`` link discovered on each page; ``by="param"``
increments a page/offset query parameter. Bounded by ``max_pages`` and guarded against the common
out-of-range CLAMP (``?page=999`` re-serving an earlier page) by a per-page content fingerprint --
a repeated page-set stops the walk rather than looping forever. Cursor/keyset advance, interacted
(load-more / infinite scroll) pagers, and the semantics flags (ordered/filtered/live) are later
phases.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal, cast
from urllib.parse import urljoin

from ...dom import clean_href
from ..web_core import Backing
from .html import tree

if TYPE_CHECKING:
    from ..reference import Reference
    from . import Document

#: a hard ceiling so a pathological advance rule can never fetch without bound.
_MAX_PAGES_CAP = 200


def _page_fingerprint(doc: "Document") -> int:
    """A cheap content fingerprint of a page, to detect an out-of-range CLAMP (a server that
    re-serves an earlier page for an over-range index) -- a repeated fingerprint stops the walk."""
    return hash(doc.content or b"")


def _ref_of(doc: "Document") -> "Reference":
    """The reference that produced ``doc`` (for deriving the next page's URL), rebuilt from its
    URL when the producing reference wasn't retained."""
    from ..reference import from_url

    return doc._ref if doc._ref is not None else from_url(doc.final_url or doc.url)


class PaginateBacking(Backing):
    """The ``paginate`` op: from a resolved page, follow the dataset's pages into one
    ``Collection[Document]``. Sequential + HTTP; ``by="link"`` (rel=next) / ``by="param"`` (a
    page/offset param). Bounded and clamp-guarded."""

    provides = frozenset({"paginate"})
    io = frozenset({"paginate"})  # fetches subsequent pages -> async / bridged
    collections = frozenset({"paginate"})  # returns a Collection of documents
    gate = "ok"

    def applies(self, core: "Document") -> bool:
        """In play for any resolved document -- pagination starts from a fetched page one."""
        return True

    def _next_ref(
        self, current: "Document", *, by: str, name: str, size: int, start: int, step: int, index: int
    ) -> "Reference | None":
        """The reference for the page AFTER ``current`` (the ``index``-th already collected), or
        ``None`` to stop. ``by="link"`` reads a ``rel="next"`` link off ``current``; ``by="param"``
        computes the next ``?name=`` value (a page number, or an offset when ``size`` is set)."""
        if by == "param":
            value = (start + index * size) if size else (start + index * step)
            return cast("Reference", _ref_of(current).dispatch("with_params", **{name: str(value)}))
        # by == "link": a discovered rel=next anchor/link on the page
        root = tree(current)
        if root is None:
            return None
        for node in root.cssselect('a[rel="next"], link[rel="next"]'):
            href = clean_href(node.get("href"))
            if href:
                from ..reference import from_url

                return from_url(urljoin(current.final_url or current.url, href))
        return None

    async def paginate(
        self,
        core: "Document",
        *,
        by: "Literal['link', 'param']" = "link",
        max_pages: int = 20,
        name: str = "page",
        start: int = 1,
        step: int = 1,
        size: int = 0,
    ) -> "list[Document]":
        """The pages of this dataset as documents, page one first. Fetches each next page
        (``by="link"`` follows ``rel=next``; ``by="param"`` walks ``?{name}=`` from ``start`` by
        ``step``, or by ``size`` as an offset) until there is no next page, a page comes back
        empty/not-ok, a page REPEATS an earlier one (an out-of-range clamp), or ``max_pages`` is
        reached. The result is a ``Collection[Document]``; chain ``select_all(...).extract(...)
        .project()`` to extract the whole dataset (the body runs across every page)."""
        bound = max(1, min(max_pages, _MAX_PAGES_CAP))
        pages: list[Document] = [core]
        seen = {_page_fingerprint(core)}
        current = core
        while len(pages) < bound:
            nxt = self._next_ref(
                current, by=by, name=name, size=size, start=start, step=step, index=len(pages)
            )
            if nxt is None:
                break
            page = await core._client.afetch(nxt, optional=True)
            if not (page.ok and page.content):
                break  # ran off the end (a 404 / empty page)
            fp = _page_fingerprint(page)
            if fp in seen:
                break  # the same page again -> an out-of-range clamp; stop rather than loop
            seen.add(fp)
            pages.append(page)
            current = page
        return pages


__all__ = ["PaginateBacking"]
