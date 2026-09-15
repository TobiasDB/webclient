"""Expr: the lazy recorder -- Core-agnostic, records into a ``Plan``.

Every attribute access, call or operator on an ``Expr`` returns a new ``Expr``
extending its plan; nothing runs until the executor walks it (via
``Expr.collect`` / ``WebClient.execute``). The recorder knows nothing about the
cores or surfaces -- the one safety boundary is that a ``_``-prefixed name is
never recordable. Static types come from the generated surface stubs the roots
are cast to (see ``scripts.gen_stubs``); at runtime every value in a chain is an
``Expr``.
"""

from __future__ import annotations

from typing import Any, NoReturn, TypeVar, cast

from .plan import _CTX, FUNCTIONS, ROOTS, Arg, Plan, Step

T = TypeVar("T")


class _Missing:
    pass


_MISSING: Any = _Missing()


class Expr:
    """A recorded chain rooted at a ``Plan`` (optionally bound to a client)."""

    __slots__ = ("_plan", "_client", "_context")
    _plan: Plan  # declared so mypy reads these, not the recording __getattr__
    _client: Any
    _context: Any  # a materialised surface this recorder is bound to (doc.lazy)

    def __init__(self, plan: Plan, client: Any = None, context: Any = None) -> None:
        object.__setattr__(self, "_plan", plan)
        object.__setattr__(self, "_client", client)
        object.__setattr__(self, "_context", context)

    # -- recording -----------------------------------------------------------
    def _extend(self, step: Step) -> "Expr":
        return Expr(self._plan.extend(step), self._client, self._context)

    def __getattr__(self, name: str) -> "Expr":
        if name.startswith("_"):  # the one safety boundary
            raise AttributeError(name)
        return self._extend(Step(kind="get", name=name))

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        eager = kwargs.pop("_collect", False)  # per-call eager escape hatch
        nxt = self._extend(
            Step(
                kind="call",
                args=[to_arg(a) for a in args],
                kwargs={k: to_arg(v) for k, v in kwargs.items()},
            )
        )
        return nxt.collect() if eager else nxt

    def _op(self, name: str, other: Any = _MISSING) -> "Expr":
        args = [] if other is _MISSING else [to_arg(other)]
        return self._extend(Step(kind="op", name=name, args=args))

    def __eq__(self, o: Any) -> "Expr":  # type: ignore[override]
        return self._op("eq", o)

    def __ne__(self, o: Any) -> "Expr":  # type: ignore[override]
        return self._op("ne", o)

    def __lt__(self, o: Any) -> "Expr":
        return self._op("lt", o)

    def __le__(self, o: Any) -> "Expr":
        return self._op("le", o)

    def __gt__(self, o: Any) -> "Expr":
        return self._op("gt", o)

    def __ge__(self, o: Any) -> "Expr":
        return self._op("ge", o)

    def __and__(self, o: Any) -> "Expr":
        return self._op("and", o)

    def __or__(self, o: Any) -> "Expr":
        return self._op("or", o)

    def __invert__(self) -> "Expr":
        return self._op("not")

    __hash__ = None  # type: ignore[assignment]

    def _coerce(self, what: str) -> NoReturn:
        raise TypeError(
            f"a lazy expression has no {what}: it records, it does not run. "
            "Use it inside extract(...) / filter(...) or with `& | ~` -- not "
            "and/or/not/bool/len/iter."
        )

    def __bool__(self) -> bool:
        return self._coerce("truth value")

    def __len__(self) -> int:
        return self._coerce("length")

    def __iter__(self) -> Any:
        return self._coerce("iterator")

    # -- evaluation ----------------------------------------------------------
    #: the single realization path: collect/acollect/stream/astream run on the
    #: bound client (or the context's) via its ``execute`` machinery -- a remote
    #: client round-trips over HTTP, all the same call. Users never call a
    #: client's ``execute`` directly. These names are reserved (non-recordable).
    def _ctx(self, context: Any) -> Any:
        return context if context is not None else self._context

    def _client_for(self, context: Any) -> Any:
        client = self._client or getattr(context, "_client", None)
        if client is None:
            from ..core.client import default_client

            client = default_client()  # the process-local shared engine
        return client

    def collect(self, context: Any = None) -> Any:
        """Evaluate this plan and return the materialised result (sync)."""
        context = self._ctx(context)
        return self._client_for(context).execute(self, context)

    async def acollect(self, context: Any = None) -> Any:
        """The async twin of ``collect`` -- ``await lazy.acollect()`` -- runs on
        the engine loop without blocking the caller's loop."""
        context = self._ctx(context)
        return await self._client_for(context).aexecute(self, context)

    def stream(self, context: Any = None) -> Any:
        """Yield the plan's rows one at a time (sync iterator)."""
        context = self._ctx(context)
        return self._client_for(context).execute(self, context, stream=True)

    def astream(self, context: Any = None) -> Any:
        """Yield the plan's rows one at a time (async iterator)."""
        context = self._ctx(context)
        return self._client_for(context).astream(self, context)

    @property
    def is_lazy(self) -> bool:
        return True

    def to_blob(self) -> str:
        """This expression's plan as a short, url-safe blob (see
        :meth:`Plan.to_blob`) -- rebuild it with :func:`from_blob`."""
        return self._plan.to_blob()

    def explain(self) -> str:
        """A readable one-line rendering of the recorded chain (pretty-print)."""
        return self._plan.describe()

    def __repr__(self) -> str:
        return f"lazy {self._plan.describe()}"


def to_arg(value: Any) -> Arg:
    if isinstance(value, Expr):
        return Arg(plan=value._plan)
    return Arg(value=value)


def lazy(cls: type[T], *, plan: Plan | None = None, client: Any = None) -> T:
    """A recording root for ``cls`` -- statically ``cls``, at runtime an
    ``Expr``."""
    return cast(T, Expr(plan or Plan(root=cls.__name__), client))


def lazy_root(core: Any) -> "Expr":
    """A lazy recorder rooted at a materialised surface ``core`` -- exposed as its
    ``.lazy`` property (see ``WebCore.lazy``).

    An engine core (client / session -- it has ``execute``) roots a ``WebClient``
    plan bound to itself, so ``wc.lazy.fetch(url).collect()`` records the verbs and
    runs them on that engine. Any other resolved surface (a document / reference)
    binds itself as the recorder's *context*, so ``doc.lazy.select(...).collect()``
    records a chain and runs it against that document."""
    if hasattr(core, "execute"):  # an engine core drives its own authoring verbs
        return Expr(Plan(root="WebClient"), client=core)
    return Expr(Plan(), client=getattr(core, "_client", None), context=core)


def from_plan(plan: Plan | dict[str, Any] | str, client: Any = None) -> Expr:
    """Rebuild an ``Expr`` from its wire form -- a ``Plan``, its dict, or a
    ``to_blob`` string (a JSON object) -- validating its names first (the wire
    safety boundary for the service/remote)."""
    if isinstance(plan, str):
        plan = Plan.from_blob(plan)
    elif isinstance(plan, dict):
        plan = Plan.model_validate(plan)
    plan.validate_names()
    return Expr(plan, client)


def from_blob(blob: str, client: Any = None) -> Expr:
    """Rebuild an ``Expr`` from a :meth:`Plan.to_blob` string, validated -- the
    LLM-authoring path: write a plan, encode it to a blob, rebuild + validate +
    (via ``expr.explain()``) pretty-print it before running."""
    return from_plan(blob, client)


# --------------------------------------------------------------------------- #
# from_explain: parse the readable ``describe()`` form back into a plan, so an
# expression round-trips through its human-readable rendering (not only the blob).
# --------------------------------------------------------------------------- #

_SENTINEL = object()
_CMP_OPS: "dict[type, str]" = {}
_BIN_OPS: "dict[type, str]" = {}


def _init_ast_maps() -> None:
    import ast

    if _CMP_OPS:
        return
    _CMP_OPS.update({
        ast.Eq: "eq", ast.NotEq: "ne", ast.Lt: "lt", ast.LtE: "le",
        ast.Gt: "gt", ast.GtE: "ge",
    })
    _BIN_OPS.update({ast.BitAnd: "and", ast.BitOr: "or"})


def _literal(node: Any) -> Any:
    import ast

    try:
        return ast.literal_eval(node)
    except (ValueError, SyntaxError, TypeError):
        return _SENTINEL


def _node_arg(node: Any) -> Arg:
    lit = _literal(node)
    return Arg(value=lit) if lit is not _SENTINEL else Arg(plan=_node_plan(node))


def _node_plan(node: Any) -> Plan:
    """Translate one AST node of a ``describe()`` expression into a Plan."""
    import ast

    _init_ast_maps()
    if isinstance(node, ast.Name):
        if node.id == _CTX:
            return Plan(root="")
        if node.id in ROOTS:
            return Plan(root=node.id)
        raise ValueError(f"unknown plan root {node.id!r}")
    if isinstance(node, ast.Attribute):  # a property get
        return _node_plan(node.value).extend(Step(kind="get", name=node.attr))
    if isinstance(node, ast.Call):
        return _call_plan(node)
    if isinstance(node, ast.Compare):
        if len(node.ops) != 1 or type(node.ops[0]) not in _CMP_OPS:
            raise ValueError("unsupported comparison in plan expression")
        return _node_plan(node.left).extend(
            Step(kind="op", name=_CMP_OPS[type(node.ops[0])],
                 args=[_node_arg(node.comparators[0])])
        )
    if isinstance(node, ast.BinOp) and type(node.op) in _BIN_OPS:
        return _node_plan(node.left).extend(
            Step(kind="op", name=_BIN_OPS[type(node.op)], args=[_node_arg(node.right)])
        )
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Invert):
        return _node_plan(node.operand).extend(Step(kind="op", name="not"))
    raise ValueError(f"cannot interpret plan expression node {type(node).__name__}")


def _call_plan(node: Any) -> Plan:
    import ast

    args = [_node_arg(a) for a in node.args]
    kwargs = {k.arg: _node_arg(k.value) for k in node.keywords if k.arg}
    func = node.func
    if isinstance(func, ast.Attribute):  # a method call -> get(name) + call(args)
        base = _node_plan(func.value).extend(Step(kind="get", name=func.attr))
        return base.extend(Step(kind="call", args=args, kwargs=kwargs))
    if isinstance(func, ast.Name):
        if func.id == "reference":  # a sourced root: reference("url")
            from ..core.reference import from_url

            url = _literal(node.args[0]) if node.args else ""
            return Plan(root="Reference", source=from_url(str(url)).model_dump())
        if func.id in FUNCTIONS:  # is_ok(operand, …) -> operand chain + fn step
            if not node.args:
                raise ValueError(f"{func.id}() needs an operand")
            base = _node_plan(node.args[0])
            rest = [_node_arg(a) for a in node.args[1:]]
            return base.extend(Step(kind="fn", name=func.id, args=rest, kwargs=kwargs))
        if func.id == "when":
            return Plan(steps=[Step(kind="when", name="", args=args, kwargs=kwargs)])
    raise ValueError("unsupported call in plan expression")


def from_explain(text: str, client: Any = None) -> Expr:
    """Rebuild an ``Expr`` from the readable :meth:`Expr.explain` / :meth:`Plan.describe`
    form -- the inverse of ``explain``, so an expression round-trips through its
    human-readable rendering as well as through :meth:`to_blob`. Parses the text as a
    Python expression (``ast``) and translates it into a validated plan. (URL-only for
    a ``reference(url)`` root -- header/cookie specs need the lossless blob.)"""
    import ast

    try:
        tree = ast.parse(text.strip(), mode="eval")
    except SyntaxError as exc:
        raise ValueError(f"cannot parse plan expression: {exc}") from exc
    plan = _node_plan(tree.body)
    plan.validate_names()
    return Expr(plan, client)


__all__ = [
    "Expr",
    "lazy",
    "lazy_root",
    "from_plan",
    "from_blob",
    "from_explain",
    "to_arg",
]
