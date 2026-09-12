"""Client facades: one set of plan builders, three executions.

Every user-facing helper builds a lazy expression (a ``Plan``) and hands it
to ``self._run``, which a subclass runs sync (:class:`WebClient`), async
(:class:`AsyncWebClient`) or remote (``RemoteWebClient``). Same interface,
different executions, all through a core that keeps only ``resolve`` +
``execute``. The builders are free functions so the facades *and*
``Session`` share them. This module imports only ``document`` + ``lazy``
(never the engine core), so ``RemoteWebClient`` builds plans with
lxml/playwright absent.
"""
from __future__ import annotations

import functools
from typing import Any, Callable
from urllib.parse import quote_plus

from .core.base import RAISE, RETURN
from .lazy import expr as _lz


# -- plan builders (shared by the facades and Session) ---------------------- #

def fetch_expr(*, browser: bool = False, optional: bool = False,
               **options: Any) -> Any:
    """``ref.resolve(...)``. The explicit error policy makes a plan (RETURN by
    default) still raise on a hard fetch unless the caller opted out."""
    return _lz.ref.resolve(browser=browser, optional=optional,
                           error=RETURN if optional else RAISE, **options)


def search_expr(engine: Any, *, limit: int = 5) -> Any:
    """Resolve the search page, then project a title/url row per result."""
    doc = _lz.doc
    return (_lz.ref.resolve().select_all(engine.result, limit=limit)
            .extract(title=doc.select(engine.title).attr("text"),
                     url=doc.select(engine.link).attr("href"))
            .project())


def summary_expr(*, browser: bool = False) -> Any:
    """Resolve a page to a compact title + markdown record."""
    doc = _lz.doc
    return (_lz.ref.resolve(browser=browser)
            .extract(url=doc.final_url, ok=doc.is_ok(),
                     title=doc.title, markdown=doc.render("markdown"))
            .project())


def default_engine() -> Any:
    from .core.webclient import SearchEngine
    return SearchEngine()


def run_on_core(core: Any, expr: Any, context: Any = None, *,
                stream: bool = False) -> Any:
    """Bridge a lazy expression onto the core's engine loop and block (sync)."""
    loop = core._ensure_loop()
    if stream:
        pool = getattr(core, "pool", None)
        return loop.stream(core.astream(expr, context),
                           buffer=max(1, getattr(pool, "max_http", 8)))
    return loop.run(core.execute(expr, context))


# -- the dispatch decorator + the shared facade ----------------------------- #

def plan_op(build: Callable[..., Any]) -> Callable[..., Any]:
    """Dispatch a plan builder: ``build`` returns an ``Expr`` (or ``(Expr,
    context)``); the wrapper hands it to ``self._run`` (sync/async/remote)."""
    @functools.wraps(build)
    def dispatch(self: "_Facade", *args: Any, **kwargs: Any) -> Any:
        built = build(self, *args, **kwargs)
        expr, context = built if isinstance(built, tuple) else (built, None)
        return self._run(expr, context)
    return dispatch


class _Facade:
    """The user surface: the same plan builders over any execution. A subclass
    supplies ``_core``/``ref``/``_run`` (and ``session`` for a session-bound
    surface)."""

    _core: Any

    def ref(self, url: str, method: str = "get", **kwargs: Any) -> Any:
        raise NotImplementedError

    def _run(self, expr: Any, context: Any = None, *, stream: bool = False) -> Any:
        raise NotImplementedError

    def _bind(self, ref: Any, session: Any = None) -> Any:
        """Make a caller-supplied reference resolvable against this client;
        remote refs already carry what they need. Overridden locally."""
        return ref

    def _context(self, ref: Any, session: Any = None, **kwargs: Any) -> Any:
        """A resolution context: a URL becomes a reference via the session (if
        any) or this client; a given reference is made usable via ``_bind``."""
        if isinstance(ref, str):
            return (session or self).ref(ref, **kwargs)
        return self._bind(ref, session)

    def execute(self, expr: Any, context: Any = None, *,
                stream: bool = False) -> Any:
        """Run a lazy expression. ``stream=True`` yields rows as they land."""
        return self._run(expr, context, stream=stream)

    def fetch(self, ref: Any, *, browser: bool = False, session: Any = None,
              optional: bool = False, **options: Any) -> Any:
        """Lazy (PLAN §8): returns a Document expression bound to this client's
        core. Run it with ``.collect()`` (or ``wc.execute``) -- was eager. The
        plan self-carries the request spec, so ``collect()`` needs no context."""
        from .lazy.expr import Expr, Plan
        context = self._context(ref, session)              # a bound Reference
        root = Expr(Plan(root="Reference", source=context.request_fields()),
                    self._core)
        return root.resolve(browser=browser, optional=optional,
                            error=RETURN if optional else RAISE, **options)

    @plan_op
    def search(self, term: str, *, engine: Any = None, limit: int = 5,
               session: Any = None) -> Any:
        engine = engine or default_engine()
        context = self._context(engine.url.format(q=quote_plus(term)), session)
        return search_expr(engine, limit=limit), context

    @plan_op
    def summary(self, url: str, *, browser: bool = False,
                session: Any = None, **reference_like: Any) -> Any:
        return (summary_expr(browser=browser),
                self._context(url, session, **reference_like))
