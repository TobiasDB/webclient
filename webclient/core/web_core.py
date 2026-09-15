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

from typing import TYPE_CHECKING, Any, ClassVar, Self, cast

from pydantic import BaseModel

from ..collection import Collection, Field
from ..query.expr import Expr, lazy_root
from ..query.plan import Plan

#: per-op remote round-trips (on server-side handles) before nudging toward .lazy.
_CHATTY_ROUND_TRIPS = 4


class UnsupportedOp(TypeError):
    """An op no chosen backing provides (the receiver lacks the capability)."""

    def __init__(self, op: str, have: frozenset[str]) -> None:
        super().__init__(
            f"{op!r} is not available here; this core has "
            f"{sorted(have) or 'no capabilities'}"
        )
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
    #: browser page scripts this backing wants installed on live pages (a
    #: ``clients.PageScript`` each -- ``init`` before nav / ``load`` after). The
    #: client gathers them (``WebClient._browser_scripts``) and the browser
    #: client installs them; the backing owns the *what*, the client the *how*.
    page_scripts: ClassVar[tuple[Any, ...]] = ()
    gate: ClassVar[str] = "ok"

    def applies(self, core: Any) -> bool:  # Any: subclasses narrow to their core
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
        client = getattr(self, "_client", None) or self
        backings = getattr(client, "_backings", None)
        if backings is None:
            raise TypeError(
                f"{type(self).__name__} has no client to register a backing on; "
                "call use() on a client/session or a client-bound surface"
            )
        backings.insert(0, backing)  # newest first -> wins the choice
        return self

    def _extra_backings(self) -> tuple[Backing, ...]:
        """Backings registered on this core's client via ``use(backing)`` -- chosen
        BEFORE the built-in ``BACKINGS`` (so a registered backing overrides /
        extends any op) for the client's CONTENT cores (documents / references).
        The engine cores themselves (client / session -- they have no ``_client``)
        get none, so a document-render backing is never probed against a client
        that has no ``kind``."""
        client = getattr(self, "_client", None)
        return tuple(getattr(client, "_backings", ())) if client is not None else ()

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
        result = getattr(self.backing(op), op)(self, *args, **kwargs)
        if op in type(self).io_ops():
            return self._bridge_io(result)
        return result

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
                except Exception:
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
        for b in self._lifecycle("aenter"):
            self._bridge_io(b.aenter(self))
        return self

    def __exit__(self, *exc: Any) -> None:
        for b in self._lifecycle("aexit"):
            self._bridge_io(b.aexit(self, *exc))

    async def __aenter__(self) -> "Self":
        for b in self._lifecycle("aenter"):
            await b.aenter(self)
        return self

    async def __aexit__(self, *exc: Any) -> None:
        for b in self._lifecycle("aexit"):
            await b.aexit(self, *exc)

    # -- realization: an eager value is already realised -----------------------
    def collect(self, context: Any = None) -> "Self":
        """An eager surface is already materialised, so ``collect`` is identity
        (the lazy recorder's ``collect`` runs the plan; this is the eager twin so
        the same ``x.collect()`` works whether ``x`` is eager or lazy)."""
        return self

    async def acollect(self, context: Any = None) -> "Self":
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
        """This core's dispatch mode -- its own if it is a client, else its bound
        client's (``sync`` / ``async`` / ``remote``). The one place a core learns
        how its ops execute."""
        client = getattr(self, "_client", None) or self
        return cast(str, getattr(client, "_mode", "sync"))

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
        return Expr(Plan(root="Reference", source=me.model_dump()), client)

    def _remote_call(self, op: str, is_prop: bool) -> Any:
        """Run ``op`` on the server: record it onto this core's remote root and
        ``collect`` (one round-trip), returning the materialised value/core --
        the SAME interface as the local dispatcher."""
        root = self._remote_root()
        if is_prop:
            value = _unwrap_remote(getattr(root, op).collect())
            self._note_remote_hop()
            return value

        def _call(*args: Any, **kwargs: Any) -> Any:
            kwargs.pop("_collect", None)
            value = _unwrap_remote(getattr(root, op)(*args, **kwargs).collect())
            self._note_remote_hop()
            return value

        return _call

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
        except Exception:
            return
        if hops == _CHATTY_ROUND_TRIPS and not getattr(client, "_nagged", False):
            client._nagged = True
            import logging

            logging.getLogger("webclient").warning(
                "remote client made %d per-op round-trips; batch a chain or "
                "fan-out with .lazy -- e.g. doc.lazy.select(...).text_content"
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
                    if is_prop:
                        return _wrap_result(self.dispatch(name))

                    def _call(*args: Any, **kwargs: Any) -> Any:
                        # an eager op is already materialised, so the lazy
                        # recorder's per-call ``_collect=True`` escape hatch is a
                        # no-op here (drop it before it reaches the backing).
                        kwargs.pop("_collect", None)
                        return _wrap_result(self.dispatch(name, *args, **kwargs))

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


def _wrap_result(value: Any) -> Any:
    """Present a dispatch result as an eager value: a list of cores becomes a
    ``Collection`` (so the row-shaping ops apply); a single core is already its
    own surface; anything else (a ``Field``/scalar) passes through."""
    if isinstance(value, (list, tuple)) and any(isinstance(v, WebCore) for v in value):
        owner = getattr(value[0], "_client", None)
        root = getattr(value[0], "root", "") or getattr(value[0], "name", "")
        return Collection(list(value), client=owner, root=root)
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
