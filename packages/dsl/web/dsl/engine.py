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

from web.fetch import BrowserFetcher, Request
from web.kernel import WebException, err
from web.resolve import Resolver, document

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

    async def acollect(self) -> list[Any]:
        from web.crawl import Crawler, Goal

        goal = Goal(start=self._seeds, max_pages=self._max_pages)
        return [d async for d in Crawler(self._engine.resolver).crawl(goal)]

    def collect(self) -> list[Any]:
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
        page = await self.browser.open(Request(url=plan.url))
        try:
            for a in plan.actions:
                page = await getattr(page, a.op)(*a.args)
            return document(await page.snapshot())
        finally:
            await page.close()

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
    """Apply a read, fanning out over a collection. ``project`` maps each element to a row dict
    (field -> the text of its sub-selector); any other read maps element-wise over a list."""
    if step.op == "project":
        fields: dict[str, str] = step.args[0]
        rows = obj if isinstance(obj, list) else [obj]
        return [{k: (e.text if (e := el.select(v)) is not None else None) for k, v in fields.items()} for el in rows]
    if isinstance(obj, list):
        return [_one(el, step) for el in obj]
    return _one(obj, step)


async def run_blob(blob: str, resolver: Resolver) -> Any:
    """API/remote dispatch, server side: rebuild a plan from its blob and run it locally."""
    return await DSL(resolver).run(Plan.from_blob(blob))


__all__ = ["DSL", "Reference", "Document", "Crawl", "run_blob"]
