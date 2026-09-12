"""Client facades: build lazy expressions, run them with ``.collect()``.

The ergonomic helpers (``fetch``/``search``/``summary``) are now *lazy*
(PLAN §8): each returns a Document/rows expression bound to the client's core
via ``_rooted`` (the request spec is embedded, so the plan is self-contained).
Run it with ``.collect()`` (sync), ``await ac.execute(...)`` (async), or
``wc.execute``. ``execute`` is the one method that runs immediately, through
``self._run`` (sync-bridge / await / remote POST per subclass). The core keeps
only ``resolve`` + ``execute``; this module imports only ``document`` +
``lazy`` (never the engine core), so the remote core builds plans with
lxml/playwright absent.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast
from urllib.parse import quote_plus

from .base import RAISE, RETURN
from . import expr as _lz
from .expr import Expr, Plan

if TYPE_CHECKING:
    from ..stubs import LazyCollection, LazyDocument, LazyReference


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


def default_engine() -> Any:
    from .webclient import SearchEngine
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


# -- the shared facade ------------------------------------------------------ #

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
        """A real ``Reference`` whose request spec roots the lazy plan
        (``_rooted``). A string becomes ``Reference.from_url``; a given
        Reference is used as-is. (The public ``ref`` is lazy; this is internal
        and stays a real Reference so the plan can embed its source.)"""
        from .models import Reference
        return Reference.from_url(ref, **kwargs) if isinstance(ref, str) else ref

    def execute(self, expr: Any, context: Any = None, *,
                stream: bool = False) -> Any:
        """Run a lazy expression. The precise ``Lazy[T] -> T`` typing lives on
        the concrete clients (``WebClient`` sync, ``AsyncWebClient`` awaitable),
        since their return shapes differ; here it stays ``Any``. ``stream=True``
        yields rows as they land."""
        return self._run(expr, context, stream=stream)

    def _rooted(self, context: Any, **resolve_opts: Any) -> "LazyDocument":
        """A lazy Document expr: resolve ``context`` self-contained (its request
        spec is embedded in the plan), bound to this client's core -- so
        ``.collect()`` needs no separate context."""
        root = Expr(Plan(root="Reference", source=context.request_fields()),
                    self._core)
        return cast("LazyDocument", root.resolve(**resolve_opts))

    def fetch(self, ref: Any, *, browser: bool = False, session: Any = None,
              optional: bool = False, **options: Any) -> "LazyDocument":
        """Lazy (PLAN §8): a Document expr bound to this client; run with
        ``.collect()`` (sync), ``await ac.execute(...)`` (async), or
        ``wc.execute``. Was eager."""
        ctx = self._context(ref, session)
        return self._rooted(ctx, browser=browser, optional=optional,
                            error=RETURN if optional else RAISE, **options)

    def search(self, term: str, *, engine: Any = None, limit: int = 5,
               session: Any = None) -> "LazyCollection":
        """Lazy: a rows expr (a title/url record per result); run with
        ``.collect()``."""
        engine = engine or default_engine()
        ctx = self._context(engine.url.format(q=quote_plus(term)), session)
        rows = (self._rooted(ctx).select_all(engine.result, limit=limit)
                .extract(title=_lz.doc.select(engine.title).attr("text"),
                         url=_lz.doc.select(engine.link).attr("href")).project())
        return cast("LazyCollection", rows)

    def summary(self, url: str, *, browser: bool = False,
                session: Any = None, **reference_like: Any) -> Any:
        """Lazy: a compact title + markdown record; run with ``.collect()``."""
        ctx = self._context(url, session, **reference_like)
        return (self._rooted(ctx, browser=browser)
                .extract(url=_lz.doc.final_url, ok=_lz.doc.is_ok(),
                         title=_lz.doc.title, markdown=_lz.doc.render("markdown"))
                .project())
