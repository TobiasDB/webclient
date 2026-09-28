"""Pagination: walk one dataset's pages -- an ITERATOR over pages, with ``until`` / ``filter`` and bounds.

Pagination is the complement of crawl: crawl walks across STRUCTURE (different pages), paginate walks
across CONTENT within one structure -- an ordered series of pages that share a shape, so one extraction
authored on any page is valid on all. (The spec: ``docs/product/pagination.md``.)

A pager is ONE of five iterators (:class:`PaginationConfig`):

* ``next=<Expr>`` -- each page gives the next page's Reference (a link, the HTTP ``Link`` header via
  ``wq.doc.next_link()``); a string is a selector whose ``href`` is followed. None -> the end.
* ``pages="<param>"`` -- an integer iterator over a URL param: ``start`` (default: the current URL's
  value, else 1 -- 0 for an offset), by ``step``, up to ``stop`` (inclusive; an int, or an Expr read
  off page one). A known ``stop`` with no ``until``/``filter`` fetches the pages concurrently.
* ``cursor=<Expr>, param="<p>"`` -- a token read off each page, carried in ``?p=``. None -> the end.
* ``click=<selector | action Expr>`` / ``scroll=True`` -- load more on the HELD live page until
  nothing more loads.

plus ``until=<Expr>`` (truthy -> this page is the last; it is kept), ``filter=<Expr>`` (a page is kept
only when truthy; the walk goes on), ``max_pages`` and ``records`` (the record selector: progress for
click/scroll, and what a repeated page is compared by).

The walk is a :class:`~webclient.loop.BoundedLoop` (:func:`pager_loop`) shared by the bound op
``doc.paginate(...)`` (it runs the loop out) and the ``wc.paginate(...)`` session (it steps it). It
always stops, with a cause: ``end`` / ``empty`` / ``repeat`` / ``until`` / ``exhausted`` / ``budget``.
Later pages are fetched on page one's tier (a page that needed the browser gets its next pages in the
browser too). Detection never runs a walk: ``doc.pagination()`` only HINTS the iterators to write.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import parse_qsl, urljoin, urlparse

from pydantic import BaseModel

from ...dom import clean_href, norm
from ..web_core import Backing
from .html import tree

if TYPE_CHECKING:
    from collections.abc import Callable

    from ...kernel.loop import BoundedLoop
    from ..reference import Reference
    from . import Document

#: a hard ceiling so a pathological pager can never fetch without bound.
_MAX_PAGES_CAP = 200
#: how many computed pages to fetch at once (the ``pages`` fast path).
_PARALLEL = 8
#: how long a click / scroll may take to show what it loaded (seconds).
_SETTLE = 5.0
#: the five iterators -- exactly one per pager.
ITERATORS = ("next", "pages", "cursor", "click", "scroll")
#: why a walk stopped.
CAUSES = ("end", "empty", "repeat", "until", "exhausted", "budget")

#: an RFC 8288 ``Link:`` header entry pointing at the next page (``<url>; rel="next"``), as GitHub
#: and other APIs paginate. The ``rel`` may sit after other link-params (``; title=…; rel=next``).
_LINK_NEXT = re.compile(r'<([^>]+)>\s*;\s*[^,]*\brel\s*=\s*"?next"?', re.I)
_SCRIPTS = re.compile(rb"<script\b.*?</script\s*>|<style\b.*?</style\s*>", re.I | re.S)

#: the removed ``by=`` API's kwargs -> what to write instead (an old plan is refused with this hint).
_REMOVED = {
    "by": "pick an iterator: next= / pages= / cursor= / click= / scroll=",
    "name": 'the param is the iterator: pages="page" (or param= with cursor=)',
    "size": 'an offset walks by its page size: pages="offset", step=20',
    "max_rows": "stop on the data instead: until=<Expr>",
    "until_before": 'until=<Expr> is the stop: until=wq.doc.select("time").attr("datetime") < "2026-01-01"',
    "cursor_attr": 'cursor= is an Expr: cursor=wq.doc.select("a.next").attr("data-after")',
    "total_pages": "stop= is the last value: stop=18 (or an Expr read off page one)",
    "action": 'click="button.more" / click=wq.doc.click("button.more") / scroll=True',
    "partition_param": "run one pager per filter value (a param on the reference)",
    "partition_values": "run one pager per filter value (a param on the reference)",
    "key": 'records="<selector>" -- a repeat is compared by the records',
}


def _invalid(message: str) -> Exception:
    from ...kernel.errors import WebException, make

    return WebException(make("paginate.invalid", message))


class PaginationConfig(BaseModel):
    """A pager: exactly ONE iterator (``next`` / ``pages`` / ``cursor`` / ``click`` / ``scroll``), plus
    ``until`` / ``filter`` and the bounds. The flat kwargs of ``doc.paginate(...)`` and
    ``wc.paginate(source, ...)`` -- the two walk a source identically."""

    model_config = {"arbitrary_types_allowed": True}

    next: Any = None  # Expr -> the next page's Reference / URL; a str is a selector whose href is followed
    pages: str = ""  # the URL param an integer iterator walks
    start: int | None = None  # pages: the first value (None: the current URL's, else 1 / 0 for an offset)
    step: int = 1  # pages: the increment (an offset's page size)
    stop: Any = None  # pages: the last value, inclusive -- an int, or an Expr read off page one
    cursor: Any = None  # Expr -> the next page's token (a str: a selector / JSON path read as text)
    param: str = ""  # cursor: the URL param the token is carried in
    click: Any = None  # a selector to click, or an action Expr run on the held page
    scroll: bool = False  # scroll the held page to load more
    until: Any = None  # Expr: truthy -> this page is the last (kept)
    filter: Any = None  # Expr: a page is kept only when truthy (the walk goes on)
    max_pages: int = 20  # the page budget
    records: str = ""  # the record selector: click/scroll progress, and the repeat comparison

    @property
    def mode(self) -> str:
        """The iterator this pager uses."""
        return next((m for m in ITERATORS if self._set(m)), "")

    def _set(self, name: str) -> bool:
        v = getattr(self, name)  # an Expr overloads ==, so test a str by type
        return v is not None and v is not False and not (isinstance(v, str) and not v)

    def check(self) -> "PaginationConfig":
        """Refuse a pager that is not exactly one iterator (``paginate.invalid``, with how to write it)."""
        given = [m for m in ITERATORS if self._set(m)]
        if len(given) != 1:
            raise _invalid(
                ("pick ONE iterator, got " + " + ".join(f"{g}=" for g in given)) if given else
                "no iterator: give next= (a link), pages= (a URL param), cursor= + param=, click= or scroll=True",
            )
        if self.cursor is not None and not self.param:
            raise _invalid('cursor= needs param= -- the URL param the token rides in: cursor=..., param="after"')
        if self.step < 1:
            raise _invalid("step= must be 1 or more (an offset walks by its page size: step=20)")
        if self.mode in ("click", "scroll") and self.filter is not None:
            raise _invalid("filter= keeps fetched pages; a click / scroll pager has ONE page -- filter its records")
        if self.max_pages < 1:
            raise _invalid("max_pages= must be 1 or more")
        return self


def config_of(kwargs: "dict[str, Any]") -> PaginationConfig:
    """A checked :class:`PaginationConfig` from ``paginate(**kwargs)`` -- a removed ``by=``-API kwarg
    is refused with what to write instead."""
    old = [k for k in kwargs if k in _REMOVED]
    if old:
        raise _invalid("paginate() was redesigned: " + "; ".join(f"{k}= -> {_REMOVED[k]}" for k in old))
    unknown = [k for k in kwargs if k not in PaginationConfig.model_fields]
    if unknown:
        raise _invalid(f"paginate() has no {', '.join(k + '=' for k in unknown)}")
    return PaginationConfig(**kwargs).check()


def pager_kwargs(hint: Any = None) -> "dict[str, Any]":
    """The ``paginate(**kwargs)`` a :class:`~.models.PagerHint` describes (what its ``code`` says, as
    values) -- ``next=wq.doc.next_link()`` when there is no hint (rel=next or the Link header)."""
    from ...interface import wq

    if hint is None or (hint.mode == "next" and (hint.via == "header" or "rel" in hint.selector)):
        return {"next": wq.doc.next_link()}
    if hint.mode == "next":
        return {"next": hint.selector}
    if hint.mode == "pages":
        return {"pages": hint.param, "start": hint.start, "step": hint.step, **({"stop": hint.stop} if hint.stop else {})}
    if hint.mode == "cursor":
        return {"cursor": hint.selector, "param": hint.param}
    if hint.mode == "click":
        return {"click": hint.selector}
    return {"scroll": True}


# -- the walk's state ---------------------------------------------------------------------------


@dataclass
class Walk:
    """A walk in progress: the KEPT ``pages`` (page one first), the last page fetched (``current``, the
    one the iterator reads), how many were fetched, the repeat ledger and -- once it stopped -- the
    ``cause``. Owned by the loop's apply; the session reads it."""

    source: Any = None  # the Reference to page one (None when page one was handed in)
    pages: list[Any] = field(default_factory=list)
    current: Any = None
    fetched: int = 0
    rows: int = 0
    seen: set[Any] = field(default_factory=set)
    urls: set[str] = field(default_factory=set)
    cause: str = ""
    browser: bool = False  # page one needed the browser -> so do the rest
    start: int = 1  # pages: the resolved first value
    last: int | None = None  # pages: the resolved stop
    first: Any = None  # a page one handed in, not yet integrated


@dataclass(frozen=True)
class _Advance:
    """A round's decision: fetch ``refs`` (one, or a batch on the fast path), ``act`` (click / scroll
    the held page), or -- neither -- stop with ``cause``."""

    refs: tuple[Any, ...] = ()
    act: bool = False
    cause: str = ""


# -- reading a page -------------------------------------------------------------------------------


def _ref_of(doc: "Document") -> "Reference":
    """The reference that produced ``doc`` (for deriving the next page's URL), rebuilt from its
    URL when the producing reference wasn't retained."""
    from ..reference import from_url

    return doc._ref if doc._ref is not None else from_url(doc.final_url or doc.url)


def _with(doc: "Document", param: str, value: Any) -> "Reference":
    """``doc``'s reference with ``?param=value``."""
    return cast("Reference", _ref_of(doc).dispatch("with_params", **{param: str(value)}))


def _link_header_next(link_header: str) -> "str | None":
    """The next-page URL from an HTTP ``Link:`` header (its ``rel="next"`` entry), or ``None``."""
    m = _LINK_NEXT.search(link_header)
    return m.group(1).strip() if m else None


def _record_texts(doc: "Document", records: str) -> "list[str]":
    """The text of each record ``records`` matches on ``doc``."""
    out: list[str] = []
    for el in doc.select_all(records):
        out.append(norm(str(_plain(el.attr("text", optional=True)) or "")))
    return out


def _count(doc: "Document", records: str) -> int:
    """How much ``doc`` holds: its records when ``records`` is given, else its content's length."""
    return sum(1 for _ in doc.select_all(records)) if records else len(doc.content or b"")


def _key(doc: "Document", records: str) -> Any:
    """What a repeated page is recognised by: its records' text when ``records`` is given (so pages
    that differ only by chrome still repeat), else its content without scripts / styles (a nonce or
    a timestamp in a script does not make a clamped page new)."""
    if records:
        return ("records", tuple(_record_texts(doc, records)))
    return hashlib.sha1(_SCRIPTS.sub(b"", doc.content or b"")).hexdigest()


def _plain(value: Any) -> Any:
    from ...query.collection import Field

    return value.get() if isinstance(value, Field) else value


def _int(value: Any, what: str) -> int:
    """An evaluated ``stop`` as an int (a Field / "18" / "Page 1 of 18" -> 18)."""
    v = _plain(value)
    if isinstance(v, bool) or v is None:
        raise _invalid(f"{what} read nothing on page one")
    if isinstance(v, (int, float)):
        return int(v)
    nums = re.findall(r"\d[\d,]*", str(v))
    if not nums:
        raise _invalid(f"{what} read {v!r} on page one -- not a number")
    return int(nums[-1].replace(",", ""))


async def _eval(expr: Any, doc: "Document", client: Any, segment: str) -> Any:
    """Evaluate a pager Expr against ``doc``, addressed under the step's ``kw:<name>`` arg."""
    from ...query.executor import aevaluate, arg_segment

    with arg_segment(segment):
        return await aevaluate(expr, doc, client=client)


def _as_ref(value: Any, doc: "Document") -> "Reference | None":
    """What a ``next=`` Expr read, as the next page's Reference (None: no next page)."""
    from ..reference import Reference, from_url

    v = _plain(value)
    if v is None or v is False or (isinstance(v, str) and not v.strip()):
        return None
    if isinstance(v, Reference):
        return v if v.ok and v.url else None
    if isinstance(v, str):
        href = clean_href(v.strip())
        return from_url(urljoin(doc.final_url or doc.url, href)) if href and href != "#" else None
    if hasattr(v, "ok") and not v.ok:
        return None
    raise _invalid(f"next= must read a link (a Reference or a URL), it read a {type(v).__name__}")


async def _next_ref(walk: Walk, cfg: PaginationConfig, client: Any) -> "Reference | None":
    """The ``next`` / ``cursor`` iterator: the page after ``walk.current`` (None: the end)."""
    doc = walk.current
    if cfg.mode == "next":
        if isinstance(cfg.next, str):  # a selector whose href is the next page
            el = doc.select(cfg.next, optional=True)
            return _as_ref(el.attr("href", optional=True), doc) if el.ok else None
        return _as_ref(await _eval(cfg.next, doc, client, "kw:next"), doc)
    if isinstance(cfg.cursor, str):  # a selector / JSON path read as text
        el = doc.select(cfg.cursor, optional=True)
        token = _plain(el.attr("text", optional=True)) if el.ok else None
    else:
        token = _plain(await _eval(cfg.cursor, doc, client, "kw:cursor"))
    if token is None or token is False or str(token).strip() == "":
        return None
    return _with(doc, cfg.param, str(token).strip())


def _url_value(url: str, param: str) -> "int | None":
    """``?param=``'s integer value in ``url``, when it carries one."""
    for k, v in parse_qsl(urlparse(url).query):
        if k == param and v.strip().isdigit():
            return int(v)
    return None


def _default_start(param: str) -> int:
    from ..crawl.canon import _OFFSET_PARAMS

    return 0 if param.lower() in _OFFSET_PARAMS else 1


# -- one page in --------------------------------------------------------------------------------


async def _take(walk: Walk, cfg: PaginationConfig, page: "Document", client: Any) -> bool:
    """Integrate a fetched ``page``: an empty / failed page or a repeat ends the walk (``False``);
    else it counts toward the budget, becomes ``current``, is KEPT when ``filter`` holds, and ends
    the walk after it when ``until`` holds. Returns whether the walk goes on."""
    from ...query.executor import truthy

    if not (page.ok and page.content):
        walk.cause = "empty"
        return False
    if cfg.records and walk.fetched and not _count(page, cfg.records):
        walk.cause = "empty"  # a page past the end: the records ran out
        return False
    url = page.final_url or page.url
    k = _key(page, cfg.records)
    if k in walk.seen or (walk.fetched and url in walk.urls and cfg.mode in ("next", "cursor")):
        walk.cause = "repeat"  # an out-of-range clamp (or a pager that points back)
        return False
    walk.seen.add(k)
    walk.urls.add(url)
    walk.fetched += 1
    walk.current = page
    if walk.fetched == 1:
        walk.browser = "browser" in (page._tiers or [])
        if cfg.mode == "pages" and cfg.stop is not None:
            walk.last = cfg.stop if isinstance(cfg.stop, int) else _int(await _eval(cfg.stop, page, client, "kw:stop"), "stop=")
    if cfg.filter is None or truthy(await _eval(cfg.filter, page, client, "kw:filter")):
        walk.pages.append(page)
        walk.rows += _count(page, cfg.records) if cfg.records else 0
    if cfg.until is not None and truthy(await _eval(cfg.until, page, client, "kw:until")):
        walk.cause = "until"
        return False
    return True


async def _fetch(ref: "Reference", walk: Walk, client: Any) -> "Document":
    """Fetch a later page the way page one was fetched (the browser when page one needed it)."""
    return cast("Document", await client.afetch(ref, optional=True, browser=True if walk.browser else False))


async def _fetch_all(refs: "list[Any]", walk: Walk, client: Any) -> "list[Document]":
    """Fetch ``refs`` CONCURRENTLY, at most ``_PARALLEL`` in flight, results in input order."""
    sem = asyncio.Semaphore(_PARALLEL)

    async def one(ref: "Reference") -> "Document":
        async with sem:
            return await _fetch(ref, walk, client)

    return list(await asyncio.gather(*(one(r) for r in refs)))


async def _act(walk: Walk, cfg: PaginationConfig, client: Any) -> None:
    """One click / scroll on the held page, then wait (up to ``_SETTLE`` s) for what it loads. Nothing
    new, or the control is gone -> ``exhausted``."""
    from ...interface import wq
    from ...query.executor import aevaluate, truthy

    doc = walk.current
    if getattr(doc, "_page", None) is None:
        from ...kernel.errors import WebException, make

        raise WebException(make("paginate.not_live", "click= / scroll= load more on a HELD browser page; resolve it with browser=True"))
    action = (
        wq.doc.scroll() if cfg.scroll else
        wq.doc.click(cfg.click) if isinstance(cfg.click, str) else cfg.click
    )
    before = _count(doc, cfg.records)
    if isinstance(cfg.click, str) and not doc.select(cfg.click, optional=True).ok:
        walk.cause = "exhausted"  # the control is gone: nothing more to load
        return
    try:
        await _eval(action, doc, client, "kw:scroll" if cfg.scroll else "kw:click")
    except Exception:  # noqa: BLE001 - a gone "load more" / a failed action: nothing more to load
        walk.cause = "exhausted"
        return
    now, waited = _count(doc, cfg.records), 0.0
    while now <= before and waited < _SETTLE:  # it loads ASYNCHRONOUSLY: re-read every 0.1 s
        try:
            await aevaluate(wq.doc.wait_for(timeout=0.1), doc, client=client)
        except Exception:  # noqa: BLE001 - the page went away
            break
        waited += 0.1
        now = _count(doc, cfg.records)
    if now <= before:
        walk.cause = "exhausted"
        return
    walk.fetched += 1  # a load counts toward the budget
    walk.rows = now if cfg.records else 0
    if cfg.until is not None and truthy(await _eval(cfg.until, doc, client, "kw:until")):
        walk.cause = "until"


# -- the loop -----------------------------------------------------------------------------------


def pager_loop(walk: Walk, cfg: PaginationConfig, client: Any, *, bus: Any = None) -> "BoundedLoop[Walk, Walk, _Advance]":
    """The pager as a :class:`~webclient.loop.BoundedLoop` over ``walk``: DECIDE the next page(s) --
    page one, the iterator's next, a concurrent batch (``pages`` with a known stop and nothing to test
    per page), or a click / scroll -- or a stop cause; APPLY fetches and integrates them (:func:`_take`).
    The loop's verdict is ``done`` with the cause as its result."""
    from ...kernel.loop import BoundedLoop

    budget = max(1, min(cfg.max_pages, _MAX_PAGES_CAP))

    async def decide(w: Walk) -> _Advance:
        if w.cause:
            return _Advance(cause=w.cause)
        if w.current is None:  # round 0: page one
            return _Advance(refs=(w.source,))
        if w.fetched >= budget:
            return _Advance(cause="budget")
        mode = cfg.mode
        if mode in ("click", "scroll"):
            return _Advance(act=True)
        if mode == "pages":
            nxt = w.start + w.fetched * cfg.step
            if w.last is not None and nxt > w.last:
                return _Advance(cause="end")
            if w.last is not None and cfg.until is None and cfg.filter is None:  # every page known up front
                values = range(nxt, w.last + 1, cfg.step)[: budget - w.fetched]
                return _Advance(refs=tuple(_with(w.current, cfg.pages, v) for v in values))
            return _Advance(refs=(_with(w.current, cfg.pages, nxt),))
        ref = await _next_ref(w, cfg, client)
        return _Advance(refs=(ref,)) if ref is not None else _Advance(cause="end")

    async def apply(w: Walk, adv: _Advance) -> None:
        if adv.act:
            await _act(w, cfg, client)
            return
        refs = list(adv.refs)
        pages = await _fetch_all(refs, w, client) if len(refs) > 1 else [await _fetch(refs[0], w, client)]
        for page in pages:
            if not await _take(w, cfg, page, client):
                return
        if len(adv.refs) > 1 and w.fetched < budget:
            w.cause = "end"  # the batch ran to stop=

    return BoundedLoop(
        observe=lambda w, i, err: w,
        decide=cast("Callable[[Walk], _Advance]", decide),  # async: the loop awaits it under astep / arun
        done_result=lambda adv: adv.cause if not (adv.refs or adv.act) else None,
        apply=apply,
        max_rounds=budget + 2,  # the walk's own budget stops it first ("budget")
        name="paginate",
        bus=bus,
    )


def seed(cfg: PaginationConfig, page_one: "Document | None" = None, source: Any = None) -> Walk:
    """A fresh :class:`Walk`: from ``page_one`` in hand (``doc.paginate``), or a ``source`` to fetch.
    A ``pages`` walk starts at ``start`` -- when page one's URL is on another value, the walk fetches
    ``start`` as its page one instead."""
    w = Walk(source=source)
    if cfg.mode == "pages":
        url = (page_one.final_url or page_one.url) if page_one is not None else str(getattr(source, "url", source) or "")
        at = _url_value(url, cfg.pages)
        w.start = cfg.start if cfg.start is not None else (at if at is not None else _default_start(cfg.pages))
        if (at if at is not None else _default_start(cfg.pages)) != w.start:  # page one is on another value
            from ..reference import from_url

            base = _ref_of(page_one) if page_one is not None else (source if not isinstance(source, str) else from_url(source))
            w.source, page_one = cast("Reference", base.dispatch("with_params", **{cfg.pages: str(w.start)})), None
    if page_one is not None:
        w.source = _ref_of(page_one)
        w.first = page_one
    return w


async def take_first(walk: Walk, cfg: PaginationConfig, client: Any) -> None:
    """Integrate a page one handed in (``doc.paginate`` starts from the page it runs on)."""
    one, walk.first = walk.first, None
    if one is not None:
        await _take(walk, cfg, one, client)


async def walk(doc: "Document", cfg: PaginationConfig, *, client: Any = None) -> Walk:
    """Run a pager out from ``doc`` (the bound op's body): the finished :class:`Walk`."""
    client = client if client is not None else doc._client
    w = seed(cfg, page_one=doc)
    prev = getattr(doc, "_keep_alive", False)
    if cfg.mode in ("click", "scroll"):  # the held page itself loads more: keep it alive across the actions
        doc._keep_alive = True
    try:
        await take_first(w, cfg, client)
        loop = pager_loop(w, cfg, client, bus=getattr(client, "bus", None))
        await loop.arun(w)
    finally:
        if cfg.mode in ("click", "scroll"):
            doc._keep_alive = prev
    if cfg.mode in ("click", "scroll") and not w.pages:
        w.pages = [doc]
    return w


class PaginateBacking(Backing):
    """The ``next_link`` op: "where is the next page of this dataset?" -- the ``next=`` iterator's
    reader for the HTTP ``Link`` header and ``rel=next`` (``next=wq.doc.next_link()``). (The walk
    itself is the bound op ``Document.apaginate``; see :func:`walk`.)"""

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


__all__ = ["CAUSES", "ITERATORS", "PaginateBacking", "PaginationConfig", "Walk", "config_of", "pager_kwargs", "pager_loop", "seed", "walk"]
