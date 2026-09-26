"""WebCore: the shared core machinery -- Capabilities + Dispatch + Backing +
Choose (rewrite foundation).

Every core (client / session / document / reference) is a ``WebCore``: it holds
a set of ``Backing``s, CHOOSES which apply to its current state, and DISPATCHES
an op to the first chosen backing that ``provides`` it. Its capabilities are
the union of the chosen backings' gates. This generalises the (clean) backing
dispatch that ``Document`` used, so all four cores share one mechanism.

The cores carry data ("Core Fields") and talk to each other; the user-facing
surface (``LazyDocument`` / ``Document`` / ...) is GENERATED from a core's
fields + its backings' ops (see ``scripts.gen_stubs``), never hand-written.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, ClassVar, Self, cast

from pydantic import BaseModel

from ..query.collection import Collection, Field
from ..errors import WebError, WebException
from ..query.expr import Expr, lazy_root
from ..query.plan import Plan

log = logging.getLogger(__name__)

#: the eager ops a recording session mirrors into its Plan (see ``WebClient.record``):
#: the navigations that root a page journey and the live interactions that advance it.
#: Reads (``select``/``attr``/``title``/...) are never recorded -- they don't change state.
#: the ops a recording records as READS of what it reached (see ``client.recording``)
READ_OPS = frozenset({"select", "select_all", "attr", "text_content", "links", "resolve"})

_RECORDABLE_OPS = frozenset(
    {"resolve", "fetch", "click", "write", "wait_for", "goto", "scroll"}
)


class UnsupportedOp(WebException, TypeError):
    """An op no chosen backing provides (the receiver lacks the capability, e.g.
    ``markdown()``/``title`` on a json document). Both a ``WebException`` -- so one
    ``except WebException`` catches it alongside fetch/select failures, with a
    structured ``.error`` -- and a ``TypeError`` (back-compat)."""

    def __init__(self, op: str, have: frozenset[str]) -> None:
        msg = (
            f"{op!r} is not available here; this core has "
            f"{sorted(have) or 'no capabilities'}"
        )
        from ..errors import make

        WebException.__init__(self, make("op.unsupported", msg, op=op))
        self.op, self.have = op, have


class Backing:
    """Serves a family of ops for one medium. ``provides`` names the ops it
    implements (each a ``method(self, core, *args, **kwargs)``); ``gate`` is the
    capability it grants when chosen; ``applies`` decides if it is in play for a
    given core's current state (default: always)."""

    provides: ClassVar[frozenset[str]] = frozenset()  # call ops: obj.op(...)
    props: ClassVar[frozenset[str]] = frozenset()  # property ops: obj.op
    #: the ops that cross the IO bridge (``resolve``/``fetch``/``summary``): under
    #: an async dispatcher they hand back an awaitable, so the async surface stub
    #: types them ``async def``. Everything else is in-memory (sync) either way.
    io: ClassVar[frozenset[str]] = frozenset()
    #: ops that return a Collection of cores (e.g. ``select_all``): declared so the
    #: eager surface wraps even an EMPTY result into a ``Collection`` (an empty list
    #: has no member to detect), keeping the type honest so a downstream
    #: ``.extract()/.project()`` never hits a bare ``list``.
    collections: ClassVar[frozenset[str]] = frozenset()
    #: browser page scripts this backing wants installed on live pages (a
    #: ``clients.PageScript`` each -- ``init`` before nav / ``load`` after). The
    #: client gathers them (``WebClient._browser_scripts``) and the browser
    #: client installs them; the backing owns the *what*, the client the *how*.
    page_scripts: ClassVar[tuple[Any, ...]] = ()
    gate: ClassVar[str] = "ok"

    def applies(self, core: Any) -> bool:  # Any: subclasses narrow to their core
        """Whether this backing is in play for ``core``'s current state (default: always).
        Probed cheaply against every core the client owns, so keep it defensive."""
        return True

    def on_load(self, core: Any, result: Any) -> None:
        """Hook: a live browser page finished loading. The client produces the raw
        page (``clients.PageResult``) and fires this on each chosen backing; a
        backing that instruments live pages (``LiveBacking``) turns the raw
        console/network signals into events here. Default: nothing. So the client
        never needs to know how a backing shapes events."""

    #: lifecycle hooks -- when the core is used as a context manager, ``WebCore``'s
    #: ``__enter__``/``__aenter__`` fire ``aenter`` on each chosen backing and its
    #: ``__exit__``/``__aexit__`` fire ``aexit`` (a backing that owns a scoped
    #: resource, e.g. ``CrawlBacking``, uses these). Both are ``async`` and bridged
    #: like an IO op, so the sync ``with`` and async ``async with`` forms both work.
    async def aenter(self, core: Any) -> None:
        """Hook: the core's context is opening (``with``/``async with``). Default: nothing."""

    async def aexit(self, core: Any, *exc: Any) -> None:
        """Hook: the core's context is closing. Default: nothing."""


class WebCore:
    """Capabilities + Dispatch + Backing + Choose.

    Subclasses declare their backings in ``BACKINGS`` (and their Core Fields as
    attributes / a pydantic model -- TBD in the rewrite). Everything a core can
    *do* comes from a backing and is reached through ``dispatch``; the cores
    themselves hold state and wiring, not op logic."""

    #: the backings this kind of core can use, in priority order (first that
    #: provides an op wins).
    BACKINGS: ClassVar[tuple[Backing, ...]] = ()

    #: per-class memoised op tables (derived from BACKINGS; see ops/prop_ops/io_ops).
    _ops_memo: ClassVar["dict[str, Backing] | None"] = None
    _prop_ops_memo: ClassVar["dict[str, Backing] | None"] = None
    _io_ops_memo: ClassVar["frozenset[str] | None"] = None
    _collection_ops_memo: ClassVar["frozenset[str] | None"] = None

    # -- engine binding ------------------------------------------------------
    def _bound_engine(self) -> Any:
        """The :class:`~.engine.Engine` this core is bound to -- its shared loop /
        pool / bus / registered backings -- or ``None`` if the core is unbound. A
        client/session resolves its own engine (parent-or-self); a document/reference
        resolves it through the client/session it is bound to. The one place a
        content core reaches the shared engine, replacing the old ``_client``-hop for
        backings."""
        owner = getattr(self, "_session", None) or getattr(self, "_client", None) or self
        resolver = getattr(owner, "_the_engine", None)
        if resolver is not None:  # a client/session knows how to resolve its engine
            return resolver()
        return getattr(owner, "_engine", None)  # an unbound doc/ref -> None

    # -- choose / capabilities ----------------------------------------------
    def use(self, backing: "Backing") -> Self:
        """Register a ``Backing`` on this core's client: every core the client
        owns (documents, references, sessions -- and the client itself) chooses it
        BEFORE its built-in backings, so it overrides or extends any op it
        ``provides`` for the cores it ``applies`` to. The general extensibility
        hook, uniform on every surface (``wc.use(...)`` / ``doc.use(...)`` register
        the same place). The most recently registered backing wins. Returns
        ``self`` for chaining.

        Narrow ``provides`` to only the ops you override, or you shadow the rest:
        e.g. a ``HtmlBacking`` subclass with ``provides = frozenset({"render"})``
        overrides ``render`` (``super()`` handles the formats you don't) while
        ``select`` / ``attr`` / live interaction still reach their built-ins."""
        engine = self._bound_engine()
        if engine is None:
            raise TypeError(
                f"{type(self).__name__} has no engine to register a backing on; "
                "call use() on a client/session or a client-bound surface"
            )
        engine._backings.insert(0, backing)  # newest first -> wins the choice
        return self

    def _extra_backings(self) -> tuple[Backing, ...]:
        """Backings registered on this core's engine via ``use(backing)`` -- chosen
        BEFORE the built-in ``BACKINGS`` (so a registered backing overrides / extends
        any op) for the engine's CONTENT cores (documents / references). Only a content
        core bound to a client/session picks them up; the SESSION cores themselves
        (client / session / crawl / pagination) get none, so a document-render backing
        (whose ``applies`` reads ``.kind``) is never probed against a core that has no
        ``kind``."""
        from .session_core import SessionCore

        if isinstance(self, SessionCore):
            return ()  # a session/engine core -- content backings never apply to it
        if getattr(self, "_session", None) is None and getattr(self, "_client", None) is None:
            return ()  # an unbound content core -- nothing registered reaches it
        engine = self._bound_engine()
        return tuple(engine._backings) if engine is not None else ()

    def choose(self) -> list[Backing]:
        """The backings that apply to this core's current state, in order --
        the client's registered backings first, then the built-in ``BACKINGS``."""
        extra = self._extra_backings()  # usually empty -- avoid the concat then
        backings = (*extra, *self.BACKINGS) if extra else self.BACKINGS
        return [b for b in backings if b.applies(self)]

    def capabilities(self) -> frozenset[str]:
        """The union of the chosen backings' gates."""
        return frozenset(b.gate for b in self.choose())

    # -- dispatch ------------------------------------------------------------
    def backing(self, op: str) -> Backing:
        """The first chosen backing that provides ``op`` (call or prop) else
        ``UnsupportedOp``."""
        for b in self.choose():
            if op in b.provides or op in b.props:
                return b
        raise UnsupportedOp(op, self.capabilities())

    def dispatch(self, op: str, *args: Any, **kwargs: Any) -> Any:
        """Run ``op`` on its backing, passing this core as the receiver. An IO op
        (an ``async def`` on the backing -- see ``Backing.io``) returns a
        coroutine that is bridged onto the right dispatcher here, so backings never
        touch ``bridge`` themselves; every other op returns its value directly.

        Mode-aware, exactly like ``__getattr__``: under the remote dispatcher an op
        that needs the server round-trips instead of running locally, so ``dispatch``
        is a single entry point with the same semantics as attribute access (no
        second, mode-blind path)."""
        if self._dispatch_mode() == "remote" and self._goes_remote(op):
            is_prop = op in type(self).prop_ops()
            remote = self._remote_call(op, is_prop)
            return remote if is_prop else remote(*args, **kwargs)
        rec = self._recorder() if (op in _RECORDABLE_OPS or op in READ_OPS) else None
        if rec is not None:
            # a READ of something the recording produced (its page, an element, a link read off it): a step of
            # the recording's reads; else a navigation / interaction: a step of its journey
            at = rec._read_tree().at(self) if op in READ_OPS else None
            if at is not None:
                return self._dispatch_read(rec, at, op, args, kwargs)
            address = self._record_address(op, args, kwargs) if op in _RECORDABLE_OPS else None
            if address is not None:
                result = self._dispatch_recorded(address, op, args, kwargs)
                if op in ("resolve", "fetch") and not hasattr(result, "__await__"):
                    rec._read_tree().reset(result)  # a new journey page: nothing read of it yet
                return result
        return self._dispatch(op, args, kwargs)

    def _dispatch(self, op: str, args: Any, kwargs: Any, record: bool = True) -> Any:
        try:
            result = getattr(self.backing(op), op)(self, *args, **kwargs)
            if op in type(self).io_ops():
                result = self._bridge_io(result)
                if record and op in _RECORDABLE_OPS:
                    self._maybe_record(op, args, kwargs)
                return result
        except WebException as exc:  # the ledger: a raised error is published exactly once
            self._note_error(exc.error, op, raised=True)
            raise
        if record and op in _RECORDABLE_OPS:
            self._maybe_record(op, args, kwargs)
        return result

    def _recorder(self) -> Any:
        """The recording session this core's calls are recorded into (None off the recording path, and
        inside a plan run -- a replay never records its own derived ops)."""
        client = getattr(self, "_session", None) or getattr(self, "_client", None) or self
        if not getattr(client, "_recording", False):
            return None
        from ..query.executor import _PLAN_LIVE

        return None if _PLAN_LIVE.get() is not None else client

    def _dispatch_read(self, rec: Any, at: "tuple[str | None, tuple[int, ...]]", op: str, args: Any, kwargs: Any) -> Any:
        """Run a READ as its step in the recording's read tree (``recording.ReadTree``): stamped ``@<node>``
        and with its item (a card of a loop over cards), publishing step / result like a plan step."""
        import time

        from ..events import CURRENT_ITEM, CURRENT_STEP

        tree = rec._read_tree()
        parent, item = at
        node = tree.child(parent, op, args, kwargs)
        engine = self._bound_engine()
        bus = engine.bus if engine is not None else None
        t_step, t_item = CURRENT_STEP.set((f"@{node}",)), CURRENT_ITEM.set(item)
        t0 = time.perf_counter()
        try:
            if bus is not None:
                from ..models import PlanEvent

                sel = next((a for a in args if isinstance(a, str)), None)
                bus.publish(PlanEvent(phase="step", document_id=getattr(self, "name", None) or None,
                                      detail={"op": op, "selector": sel, "args": [a for a in args if isinstance(a, (str, int, float, bool))][:4], "recorded": True}))
            try:
                result = self._dispatch(op, args, kwargs, record=False)
            except BaseException as exc:
                self._note_recorded_result(bus, op, None, t0, exc)
                raise
            self._note_recorded_result(bus, op, result, t0)
            tree.result(node, op, item, result)
            return result
        finally:
            CURRENT_ITEM.reset(t_item); CURRENT_STEP.reset(t_step)

    def _record_address(self, op: str, args: Any, kwargs: Any) -> "str | None":
        """The address a RECORDED call takes in the recording's plan (``None`` off the recording
        path): a navigation starts the chain (its ``resolve`` step), an interaction is the op inside
        the ``.step(...)`` appended next. What the call publishes is attached to that step -- a
        recorded session's trace replays against its plan like an executed one."""
        client = getattr(self, "_session", None) or getattr(self, "_client", None) or self
        if not getattr(client, "_recording", False):
            return None
        from ..query.executor import _PLAN_LIVE

        if _PLAN_LIVE.get() is not None:
            return None
        if op in ("resolve", "fetch"):
            return "0"  # a fresh chain: Reference -> resolve
        chain = getattr(client, "_record_chain", None)
        if chain is None:
            return None
        return f"{len(chain._plan.steps)}/arg:0/0"

    def _dispatch_recorded(self, address: str, op: str, args: Any, kwargs: Any) -> Any:
        """Run a recorded call AS its plan step: the step's events are stamped with its address,
        and it publishes the same ``step`` / ``result`` events an executed plan's step does."""
        import time

        from ..events import CURRENT_STEP

        engine = self._bound_engine()
        bus = engine.bus if engine is not None else None
        token = CURRENT_STEP.set(tuple(address.split("/")))
        t0 = time.perf_counter()
        try:
            if bus is not None:
                from ..models import PlanEvent

                sel = next((a for a in args if isinstance(a, str)), None)
                bus.publish(PlanEvent(phase="step", document_id=getattr(self, "name", None) or None,
                                      detail={"op": op, "selector": sel if op not in ("fetch",) else None, "args": [a for a in args if isinstance(a, (str, int, float, bool))][:4], "recorded": True}))
            try:
                result = self._dispatch(op, args, kwargs)
            except BaseException as exc:
                self._note_recorded_result(bus, op, None, t0, exc)
                raise
            if hasattr(result, "__await__"):  # an async client: the step runs when awaited
                return self._recorded_awaitable(result, address, bus, op, t0)
            self._note_recorded_result(bus, op, result, t0)
            return result
        finally:
            CURRENT_STEP.reset(token)

    async def _recorded_awaitable(self, coro: Any, address: str, bus: Any, op: str, t0: float) -> Any:
        from ..events import CURRENT_STEP

        CURRENT_STEP.set(tuple(address.split("/")))
        try:
            result = await coro
        except BaseException as exc:
            self._note_recorded_result(bus, op, None, t0, exc)
            raise
        self._note_recorded_result(bus, op, result, t0)
        return result

    @staticmethod
    def _note_recorded_result(bus: Any, op: str, value: Any, t0: float, exc: "BaseException | None" = None) -> None:
        if bus is None:
            return
        from ..query.executor import _note_result
        from ..query.plan import Step

        _note_result(Step(kind="get", name=op), value, t0, type("_C", (), {"bus": bus})(), exc)

    def _note_error(self, error: WebError, op: str = "", *, raised: bool = False) -> WebError:
        """Record ``error`` on the ledger: bind it to this core (``op`` / ``subject``), keep it on
        the core's own ``_errors`` list when it has one, and publish an ``ErrorEvent`` on the
        bound engine's bus -- ONCE (a re-raise through several dispatch frames does not
        duplicate it). Returns the bound error. The one place errors enter the ledger, so no
        error -- raised, returned under RETURN, or swallowed by a fallback -- can disappear."""
        if error._noted:
            return error
        bound = error.bound(op=op, subject=str(getattr(self, "name", "") or ""))
        error._noted = True
        bound._noted = True
        own = getattr(self, "_errors", None)
        if isinstance(own, list):
            own.append(bound)
        engine = self._bound_engine()
        if engine is not None:
            from ..models import ErrorEvent

            engine.bus.publish(ErrorEvent(
                error=bound, raised=raised, document_id=getattr(self, "name", None) or None,
                session_id=getattr(self, "session_id", None) or None,
            ))
        return bound

    def _maybe_record(self, op: str, args: Any, kwargs: Any) -> None:
        """Mirror an eager action into the bound recording session's Plan, if any (see
        :meth:`WebClient.record`). Zero cost off the recording path -- a session is
        recording only inside a ``with wc.record()`` block. Ops that happen INSIDE a plan
        run (a ``collect``/replay, marked by the executor's live-page scope) are skipped,
        so recording a chain, or replaying one, never records the derived sub-ops."""
        client = getattr(self, "_session", None) or getattr(self, "_client", None) or self
        if not getattr(client, "_recording", False):
            return
        from ..query.executor import _PLAN_LIVE

        if _PLAN_LIVE.get() is not None:  # inside a plan evaluation -- not an authored action
            return
        cast(Any, client)._record_op(self, op, args, kwargs)

    def _bridge_io(self, coro: Any) -> Any:
        """Bridge an IO op's coroutine on the right dispatcher: a session's, else
        the bound client's, else a process-local default (bound onto this core so
        the op's own body reaches for the same one). ``bridge`` then picks blocking
        (sync) / awaitable (async) / on-loop (the executor)."""
        client = getattr(self, "_session", None) or getattr(self, "_client", None)
        if client is None:
            if hasattr(self, "bridge"):  # a client core is its own engine
                client = self
            else:  # an unbound reference/document -- give it the default client
                from .client import default_client

                client = default_client()
                try:
                    self._client = client
                except AttributeError:  # a core without the private slot -- fine
                    pass
        return cast(Any, client).bridge(coro)

    def has_op(self, op: str) -> bool:
        """Whether any chosen backing provides ``op`` (call or prop)."""
        return any(op in b.provides or op in b.props for b in self.choose())

    # -- lifecycle: a core is a context manager, delegating to its backings ---
    def _lifecycle(self, hook: str) -> "list[Backing]":
        """The chosen backings that actually override ``aenter``/``aexit`` (so a
        plain core with no lifecycle backing is a no-op context manager)."""
        base = getattr(Backing, hook)
        return [b for b in self.choose() if getattr(type(b), hook) is not base]

    def __enter__(self) -> "Self":
        """Open the core as a context manager: fire each chosen backing's ``aenter`` hook
        (bridged onto the loop). A core with no lifecycle backing is a no-op ``with``."""
        for b in self._lifecycle("aenter"):
            self._bridge_io(b.aenter(self))
        return self

    def __exit__(self, *exc: Any) -> None:
        """Close the core's context: fire each chosen backing's ``aexit`` hook (bridged)."""
        for b in self._lifecycle("aexit"):
            self._bridge_io(b.aexit(self, *exc))

    async def __aenter__(self) -> "Self":
        """The ``async with`` open: await each chosen backing's ``aenter`` on the caller's loop."""
        for b in self._lifecycle("aenter"):
            await b.aenter(self)
        return self

    async def __aexit__(self, *exc: Any) -> None:
        """The ``async with`` close: await each chosen backing's ``aexit`` on the caller's loop."""
        for b in self._lifecycle("aexit"):
            await b.aexit(self, *exc)

    # -- realization: an eager value is already realised -----------------------
    def collect(self, context: Any = None) -> "Self":
        """Identity: an eager surface is ALREADY the materialised value, so it needs
        no ``collect()`` -- ``wc.fetch(url)`` / ``ref.resolve()`` hand back a ready
        ``Document`` directly. ``collect()`` is what runs a *lazy* plan (a ``.lazy``
        chain or a ``wq`` expression); this eager twin exists only so mode-agnostic
        code can call ``x.collect()`` whether ``x`` is eager or lazy. Don't append it
        to an eager chain by habit -- it does nothing there."""
        return self

    async def acollect(self, context: Any = None) -> "Self":
        """Async twin of :meth:`collect` -- identity, since an eager surface is already
        materialised; it exists so mode-agnostic code can ``await x.acollect()`` uniformly."""
        return self

    @property
    def lazy(self) -> Any:
        """A lazy recorder rooted at this surface (bound to it so
        ``doc.lazy.select(...).collect()`` records then runs against this core).
        The generated surface stubs re-type this as the matching ``Lazy`` variant
        (``wc.lazy`` -> ``Lazy[WebClient]``, ``doc.lazy`` -> ``LazyDocument``...)."""
        return lazy_root(self)

    # -- dispatch mode (sync / async / remote), read by every core -----------
    def _dispatch_mode(self) -> str:
        """This core's dispatch mode (``sync`` / ``async`` / ``remote``) -- read from the
        ENGINE it is bound to, so every session scoped on one engine shares its mode. The
        one place a core learns how its ops execute."""
        engine = self._bound_engine()
        return cast(str, getattr(engine, "_mode", "sync"))

    def _goes_remote(self, op: str) -> bool:
        """Whether ``op`` must run on the server under the remote dispatcher: a
        server-side document handle has no local content (every content op
        round-trips), and any core's IO ops (``resolve``/``fetch``/``summary``) do
        too. Pure/in-memory ops (``ref``, ``join``, ``url``, ...) stay local."""
        return getattr(self, "_remote_handle", False) or op in type(self).io_ops()

    def _remote_root(self) -> Any:
        """A recorder rooted at this core for remote execution: a client -> a
        ``WebClient`` plan; a server-side document handle -> a plan rooted at its
        id; a reference -> a plan carrying its full spec."""
        me = cast(Any, self)
        client = getattr(self, "_client", None) or self
        if self is client:  # the remote client itself
            return Expr(Plan(root="WebClient"), client)
        if getattr(self, "_remote_handle", False):  # a server-side document
            return Expr(Plan(root="Document", source={"document_id": me.id}), client)
        from .reference import Reference

        if isinstance(self, Reference):  # a reference carries its full spec
            return Expr(Plan(root="Reference", source=me.model_dump()), client)
        # any other core has no plan-shaped remote root -- fail clearly instead of
        # shipping a malformed plan (500). (A remote Crawl doesn't reach here: it
        # overrides ``_remote_call`` to advance its server-side crawl directly.)
        raise NotImplementedError(
            f"{type(self).__name__} ops are not available on a remote client"
        )

    def _remote_call(self, op: str, is_prop: bool) -> Any:
        """Run ``op`` on the server: record it onto this core's remote root and
        ``collect`` (one round-trip), returning the materialised value/core --
        the SAME interface as the local dispatcher. A content op on a server-side
        handle that was evicted is replayed once (see :meth:`_remote_eval`)."""
        if is_prop:
            value = self._remote_eval(lambda: _unwrap_remote(getattr(self._remote_root(), op).collect()))
            self._note_remote_hop()
            return value

        def _call(*args: Any, **kwargs: Any) -> Any:
            kwargs.pop("_collect", None)
            value = self._remote_eval(
                lambda: _unwrap_remote(getattr(self._remote_root(), op)(*args, **kwargs).collect())
            )
            self._note_remote_hop()
            return value

        return _call

    def _remote_eval(self, thunk: "Callable[[], Any]") -> Any:
        """Run a remote op, replaying ONCE if the server-side handle was evicted. The
        app is stateless by choice, so a handle is reproducible: on a retriable
        ``NoSuchDocument`` we re-run the plan that produced this handle
        (``_remote_source``) to get a fresh id, then retry the op against it. Any other
        failure -- or a handle with no recorded producer -- propagates unchanged."""
        from ..errors import RemoteError

        try:
            return thunk()
        except RemoteError as exc:
            source = getattr(self, "_remote_source", None)
            err = exc.error
            if source is None or err is None or err.type != "NoSuchDocument" or not err.retriable:
                raise
            fresh = source.collect()  # re-run the producer -> a fresh server-side handle
            self.id = self.name = cast(Any, fresh).id  # adopt its id; the op re-roots on it
            return thunk()

    def _note_remote_hop(self) -> None:
        """Count a per-op remote round-trip made on a server-side handle (the
        chatty, batchable pattern -- a client verb like ``rc.fetch`` is one entry
        round-trip and does not count), and nudge toward ``.lazy`` once a chain of
        them adds up. Batching a chain/fan-out with ``.lazy`` runs it in a single
        round-trip."""
        if not getattr(self, "_remote_handle", False):
            return  # not a derived handle -- no chain to batch
        client = cast(Any, getattr(self, "_client", None) or self)
        hops = getattr(client, "_remote_hops", 0) + 1
        try:
            client._remote_hops = hops
        except AttributeError:  # not a remote client (no counter slot) -- nothing to nag
            return
        from ..settings import current

        if hops == current().limits.chatty_round_trips and not getattr(client, "_nagged", False):
            client._nagged = True
            log.warning(
                "remote client made %d per-op round-trips; batch a chain or "
                "fan-out with .lazy -- e.g. doc.lazy.select(...).attr('text')"
                ".collect() -- to run it in one round-trip",
                hops,
            )

    # -- a core IS its own eager surface -------------------------------------
    if not TYPE_CHECKING:  # hidden from type checkers -- the eager surface stubs
        # (``Document``/``Reference``) are the typed interface; a bare
        # ``__getattr__`` here would make every attribute access ``Any``.

        def __getattr__(self, name: str) -> Any:
            """A resolved core is directly usable as its eager surface: an op name
            dispatches immediately (a prop op returns its value; a call op returns
            a dispatcher), a list of cores comes back as a ``Collection``. Under
            the remote dispatcher an op that needs the server round-trips instead
            (same interface). Non-op names delegate to the next ``__getattr__`` in
            the MRO -- pydantic's, for the cores' private attrs (``_page``/...)."""
            if not name.startswith("_"):
                cls = type(self)
                is_prop = name in cls.prop_ops()
                is_call = name in cls.ops()
                if not (is_prop or is_call):  # an op a registered backing adds
                    for b in self._extra_backings():
                        is_prop = is_prop or name in b.props
                        is_call = is_call or name in b.provides
                if is_prop or is_call:
                    if self._dispatch_mode() == "remote" and self._goes_remote(name):
                        return self._remote_call(name, is_prop)
                    is_coll = name in cls.collection_ops()
                    if is_prop:
                        return _wrap_result(self.dispatch(name), self, is_coll)

                    def _call(*args: Any, **kwargs: Any) -> Any:
                        # an eager op is already materialised, so the lazy
                        # recorder's per-call ``_collect=True`` escape hatch is a
                        # no-op here (drop it before it reaches the backing).
                        kwargs.pop("_collect", None)
                        return _wrap_result(
                            self.dispatch(name, *args, **kwargs), self, is_coll
                        )

                    return _call
            # delegate to pydantic's __getattr__ (private attrs); it is a runtime
            # method not in the type stubs, so fetch it dynamically.
            pyd_getattr = getattr(BaseModel, "__getattr__", None)
            if pyd_getattr is not None:
                return pyd_getattr(self, name)
            raise AttributeError(name)

    # -- op surface (for generation) ----------------------------------------
    # These three reflect only ``cls.BACKINGS`` (a class-invariant ClassVar) yet sit
    # on the hot path -- ``dispatch``/``__getattr__`` consult them on every op and
    # every fan-out element. Memoise per class (the returned tables are read-only).
    @classmethod
    def ops(cls) -> dict[str, Backing]:
        """Every *call* op, op-name -> owning backing (first wins)."""
        memo = cls.__dict__.get("_ops_memo")
        if memo is None:
            memo = {}
            for backing in cls.BACKINGS:
                for op in backing.provides:
                    memo.setdefault(op, backing)
            cls._ops_memo = memo
        return cast("dict[str, Backing]", memo)

    @classmethod
    def prop_ops(cls) -> dict[str, Backing]:
        """Every *property* op, name -> owning backing (first wins)."""
        memo = cls.__dict__.get("_prop_ops_memo")
        if memo is None:
            memo = {}
            for backing in cls.BACKINGS:
                for op in backing.props:
                    memo.setdefault(op, backing)
            cls._prop_ops_memo = memo
        return cast("dict[str, Backing]", memo)

    @classmethod
    def collection_ops(cls) -> frozenset[str]:
        """Ops that return a Collection of cores (union of the backings'
        ``collections`` sets) -- used to force-wrap an empty result."""
        memo = cls.__dict__.get("_collection_ops_memo")
        if memo is None:
            memo = frozenset().union(*(b.collections for b in cls.BACKINGS)) if cls.BACKINGS else frozenset()
            cls._collection_ops_memo = memo
        return cast("frozenset[str]", memo)

    @classmethod
    def io_ops(cls) -> frozenset[str]:
        """The call ops that cross the IO bridge (awaitable under an async
        dispatcher) -- the union of the backings' ``io`` sets."""
        memo = cls.__dict__.get("_io_ops_memo")
        if memo is None:
            memo = (
                frozenset().union(*(b.io for b in cls.BACKINGS))
                if cls.BACKINGS
                else frozenset()
            )
            cls._io_ops_memo = memo
        return cast("frozenset[str]", memo)


def _wrap_result(value: Any, owner: Any = None, force_collection: bool = False) -> Any:
    """Present a dispatch result as an eager value: a list of cores becomes a
    ``Collection`` (so the row-shaping ops apply); a single core is already its
    own surface; anything else (a ``Field``/scalar) passes through. ``force_collection``
    wraps even an EMPTY list (a collection op that matched nothing), deriving the
    client/root from ``owner`` since there is no member to read them from -- so a
    downstream ``.extract()/.project()`` never hits a bare ``list``."""
    if isinstance(value, (list, tuple)):
        first = next((v for v in value if isinstance(v, WebCore)), None)
        if first is not None:  # derive owner/root from a real core, not value[0]
            client = getattr(first, "_client", None)
            root = getattr(first, "root", "") or getattr(first, "name", "")
            return Collection(list(value), client=client, root=root)
        if force_collection:  # an empty collection result -> an empty Collection
            # the client must be the ENGINE (has ``loop``): the owner itself if it is
            # a client, else the owner's bound client (never a Document).
            client = owner if hasattr(owner, "loop") else getattr(owner, "_client", None)
            root = getattr(owner, "name", "") or getattr(owner, "root", "")
            return Collection(list(value), client=client, root=root)
    if isinstance(value, Field):
        # the eager tier returns raw values, never a Field -- Field is a lazy/recorder
        # wrapper. Unwrap a scalar leaf (attr/regex/is_ok/...) to its value (None on a miss).
        return value.get()
    return value


def _unwrap_remote(value: Any) -> Any:
    """A remote ``collect()`` returns ``_materialize``d values (a scalar wrapped in
    a ``Field``); unwrap it back to the raw value so a remote op returns exactly
    what its local twin does (a ``str``, not a ``Field``). A list of cores still
    lifts to a ``Collection``."""
    if isinstance(value, Field):
        return value.get()
    return _wrap_result(value)


__all__ = ["WebCore", "Backing", "UnsupportedOp"]
