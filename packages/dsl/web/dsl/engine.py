"""The lazy engine and its typed surfaces -- clean separations with clean joins.

Three surfaces, one per layer, tied together by one-liners:
  * :class:`Reference` -- drive a page: ``click`` / ``type`` / ``wait_for`` return **Self**, so a
    chain of actions snapshots nothing; ``.doc()`` is the join into the Document surface.
  * :class:`Document` -- read the parsed document: ``select`` / ``select_all`` / ``text`` / ``links``.
  * :class:`Crawl` -- reach many documents from seeds.

Each surface records into a :class:`Plan`; the terminals dispatch it in a mode -- ``collect``
(sync), ``acollect`` (async), ``to_blob`` (API/remote). The plain layers wrote every method once;
:meth:`DSL.run` awaits a coroutine result and passes a plain value through, so one definition
lights up all modes.
"""

from __future__ import annotations

import asyncio
from typing import Any

from web.crawl import Crawler, Goal
from web.fetch import BrowserFetcher, Request
from web.kernel import WebException, err
from web.parse import Document as ParsedDocument
from web.resolve import Resolver, document

from . import transforms
from .plan import Plan, Step


class Reference:
    """The Reference DSL: drive a page with actions that return Self, then ``.doc()`` to read."""

    def __init__(self, engine: "DSL", url: str, actions: tuple[Step, ...] = ()) -> None:
        self._engine = engine
        self._url = url
        self._actions = actions

    def _drive(self, op: str, *args: Any) -> "Reference":
        return Reference(self._engine, self._url, (*self._actions, Step(op=op, args=list(args))))

    def click(self, selector: str) -> "Reference":
        return self._drive("click", selector)

    def type(self, selector: str, text: str) -> "Reference":
        return self._drive("type", selector, text)

    def wait_for(self, selector: str) -> "Reference":
        return self._drive("wait_for", selector)

    def doc(self) -> "Document":
        """The clean join: snapshot the driven page and parse it into the Document surface."""
        return Document(self._engine, Plan(url=self._url, actions=list(self._actions)))


class Document:
    """The Document DSL: reads over the parsed document. Terminals dispatch the whole plan."""

    def __init__(self, engine: "DSL", plan: Plan, reads: tuple[Step, ...] = ()) -> None:
        self._engine = engine
        self._plan = plan
        self._reads = reads

    def _read(self, op: str, *args: Any) -> "Document":
        return Document(self._engine, self._plan, (*self._reads, Step(op=op, args=list(args))))

    def select(self, css: str) -> "Document":
        return self._read("select", css)

    def select_all(self, css: str) -> "Document":
        return self._read("select_all", css)

    def text(self) -> "Document":
        return self._read("text")

    def links(self) -> "Document":
        return self._read("links")

    def attr(self, name: str) -> "Document":
        return self._read("attr", name)

    def project(self, **selectors: str) -> "Document":
        """Over a ``select_all`` collection, map each element to a row dict -- each field is the
        text of its sub-selector (``project(title='.t', price='.p')`` -> ``list[dict]``)."""
        return self._read("project", selectors)

    # -- post-extraction transforms: shape the extracted DATA (see :mod:`.transforms`) --
    def filter(self, **equals: Any) -> "Document":
        """Keep rows whose named fields match (substring for strings, equality otherwise)."""
        return self._read("filter", equals)

    def nonempty(self) -> "Document":
        """Drop empty rows (a dict with no truthy field; an empty/None scalar)."""
        return self._read("nonempty")

    def distinct(self, key: "str | None" = None) -> "Document":
        """Drop duplicates in order; ``key`` dedupes row dicts by one field."""
        return self._read("distinct", key)

    def limit(self, n: int) -> "Document":
        """Keep at most the first ``n`` items."""
        return self._read("limit", n)

    def merge(self) -> "Document":
        """Flatten one level -- a list of lists becomes one flat list."""
        return self._read("merge")

    def number(self, field: "str | None" = None) -> "Document":
        """Parse the first number out of each value (or one ``field`` of each row)."""
        return self._read("number", {"field": field})

    def date(self, field: "str | None" = None) -> "Document":
        """Parse a date/time out of each value (ISO 8601), optionally targeting one ``field``."""
        return self._read("date", {"field": field})

    def strip(self, field: "str | None" = None) -> "Document":
        """Strip surrounding whitespace from each value (or one ``field``)."""
        return self._read("strip", {"field": field})

    def split(self, sep: "str | None" = None, *, field: "str | None" = None) -> "Document":
        """Split each string value on ``sep`` (whitespace if omitted), optionally one ``field``."""
        return self._read("split", {"field": field, "sep": sep})

    def regex(self, pattern: str, *, group: "int | str" = 0, field: "str | None" = None) -> "Document":
        """Reduce each value to a regex match (``group`` of it), optionally one ``field``."""
        return self._read("regex", {"field": field, "pattern": pattern, "group": group})

    def _full(self) -> Plan:
        return Plan(url=self._plan.url, actions=self._plan.actions, reads=list(self._reads))

    def collect(self) -> Any:
        """SYNC dispatch: run the plan to completion (blocks; not from inside a running loop)."""
        return asyncio.run(self._engine.run(self._full()))

    async def acollect(self) -> Any:
        """ASYNC dispatch: run the plan on the caller's loop."""
        return await self._engine.run(self._full())

    def to_blob(self) -> str:
        """API/remote dispatch: the serialised plan to run on a server (see :func:`run_blob`)."""
        return self._full().to_blob()


class Crawl:
    """The Crawl DSL: reach documents from seeds (a thin lazy face over web.crawl)."""

    def __init__(self, engine: "DSL", seeds: list[str], max_pages: int) -> None:
        self._engine = engine
        self._seeds = seeds
        self._max_pages = max_pages

    async def acollect(self) -> "list[ParsedDocument]":
        goal = Goal(start=self._seeds, max_pages=self._max_pages)
        return [d async for d in Crawler(self._engine.resolver).crawl(goal)]

    def collect(self) -> "list[ParsedDocument]":
        return asyncio.run(self.acollect())


class DSL:
    """A lazy execution engine over a :class:`~web.resolve.Resolver`. ``ref(url)`` enters the
    Reference surface; ``crawl(seeds)`` the Crawl surface."""

    def __init__(self, resolver: Resolver, *, browser: BrowserFetcher | None = None) -> None:
        self.resolver = resolver
        self.browser = browser

    def ref(self, url: str) -> Reference:
        return Reference(self, url)

    def crawl(self, seeds: list[str], *, max_pages: int = 50) -> Crawl:
        return Crawl(self, list(seeds), max_pages)

    async def _root(self, plan: Plan) -> Any:
        """Obtain the root Document: drive a live page through the Reference actions and parse
        its snapshot (browser), or resolve the URL statically when there are no actions."""
        if not plan.actions:
            return await self.resolver.resolve(Request(url=plan.url))
        if self.browser is None:
            raise WebException(err("dsl.needs_browser", "Reference actions require a browser (DSL(..., browser=...))"))
        session = await self.browser.session()  # the session owns the page
        try:
            await session.goto(Request(url=plan.url))
            for a in plan.actions:
                await getattr(session, a.op)(*a.args)
            return document(await session.snapshot())
        finally:
            await session.aclose()

    async def run(self, plan: Plan) -> Any:
        """Execute a plan: obtain the root Document, then apply the Document reads. Reads are
        pure/sync; a read applied to a collection (a ``select_all`` result) maps over it."""
        obj: Any = await self._root(plan)
        for r in plan.reads:
            obj = _apply_read(obj, r)
        return obj

    async def aclose(self) -> None:
        await self.resolver.aclose()
        if self.browser is not None:
            await self.browser.aclose()


def _one(obj: Any, step: Step) -> Any:
    """Apply one read to a single object: call a method, or read a property (parse's ``text`` /
    ``links`` are properties, so a non-callable attribute is used directly)."""
    attr = getattr(obj, step.op)
    return attr(*step.args) if callable(attr) else attr


def _apply_read(obj: Any, step: Step) -> Any:
    """Apply a read. ``project`` maps each element to a row dict; the :mod:`.transforms` ops shape
    the extracted DATA; any other read is an element method fanned out over a collection."""
    if obj is None:  # a prior select missed -> the rest of the chain is None, not a crash
        return None
    if step.op == "project":
        fields: dict[str, str] = step.args[0]
        rows = obj if isinstance(obj, list) else [obj]
        return [{k: (e.text if (e := el.select(v)) is not None else None) for k, v in fields.items()} for el in rows]
    if step.op in transforms.TRANSFORMS:
        return transforms.apply(obj, step.op, step.args)
    if isinstance(obj, list):
        return [_one(el, step) if el is not None else None for el in obj]
    return _one(obj, step)


async def run_blob(blob: str, resolver: Resolver) -> Any:
    """API/remote dispatch, server side: rebuild a plan from its blob and run it locally."""
    return await DSL(resolver).run(Plan.from_blob(blob))


__all__ = ["DSL", "Reference", "Document", "Crawl", "run_blob"]
