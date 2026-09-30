"""The executor: walk a :class:`~web.dsl.plan.Plan` against the live cores.

ONE evaluator drives every dispatch mode. :func:`arun` is the async terminal (``collect`` bridges
onto it, :func:`run_blob` is the service/remote server side); it obtains the root value (resolve a
``reference(url)``, or the passed context), walks the recorded steps -- ``get`` + ``call`` dispatch
a method on the current value (fanning out over a Collection), ``op`` / ``fn`` / ``when`` fold it --
and returns the SMART shape (extracted rows -> ``list[dict]``, a field chain -> a list of values, a
single field -> its value). Sub-expressions (an ``extract`` column, a ``filter`` predicate, a
``when`` branch) are re-entrant walks against one element, so the ``wq`` chain and its sub-chains
share this one implementation. Lean by design: no browser/pool/remote/streaming baggage.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import cast

from pydantic import JsonValue
from web.fetch import Request, WebException, err
from web.parse import Document, Element, dig
from web.resolve import (
    PaginatePolicy,
    RatePolicy,
    Resolver,
    RetryPolicy,
    RotationPolicy,
)
from web.resolve import profiles as _profiles

#: the resolve-step keywords that carry a per-step POLICY (build a Resolver for that fetch); any
#: other keyword (e.g. optional) is not a policy.
_RESOLVE_POLICY = frozenset(
    {
        "profile",
        "paginate",
        "max_pages",
        "rate_limit",
        "retry",
        "rotate",
        "raise_on_error",
    }
)

from .plan import Arg, Plan, Step
from .values import Collection, Field, Ref, raw

#: attributes that read as a resolvable reference (a URL), not a plain string value.
_LINK_ATTRS = frozenset({"href", "src"})

#: a per-RUN resolve memo (URL -> Document), so following a record's link for SEVERAL detail-page
#: fields fetches that page ONCE, not once per field. A query that writes `select('a').attr('href')
#: .resolve().select(X)` for each of N detail fields would otherwise refetch the same page N times
#: (the DSL cannot bind one resolved doc to many columns). Scoped to the outermost `arun` and shared
#: by every nested sub-walk in the same run; only for policy-free resolves (a paginate/retry/rotate
#: resolve is not memoised). Never crosses runs.
_RESOLVE_CACHE: "ContextVar[dict[str, object] | None]" = ContextVar("_resolve_cache", default=None)


@contextmanager
def resolve_memo() -> "Iterator[None]":
    """Share ONE resolve memo across SEVERAL runs: inside the block every ``arun`` reuses the same
    URL -> document memo, so probing a chain repeatedly (an author testing each step) fetches each
    detail page once instead of once per run. Nested inside a run, it joins the live memo."""
    live = _RESOLVE_CACHE.get()
    token = _RESOLVE_CACHE.set({} if live is None else live)  # an EMPTY live memo is still live
    try:
        yield
    finally:
        _RESOLVE_CACHE.reset(token)


_MISSING: object = object()
#: ops whose call args stay LAZY sub-plans, evaluated per element (not once, eagerly).
_ROW_OPS = frozenset({"extract", "filter"})
#: the pseudo-attributes ``attr(name)`` maps to an element's text rather than a real attribute.
_TEXT_ATTRS = frozenset({"text", "value"})


async def arun(plan: Plan, root: object = None, *, resolver: "Resolver | None" = None) -> object:
    """Async terminal: walk ``plan`` and return its materialised, smart-shaped result. Uses
    ``resolver`` to fetch, or a transient one opened + closed for the call when omitted.
    """
    # install a per-run resolve memo for the OUTERMOST run; a nested arun reuses the live one.
    live = _RESOLVE_CACHE.get()
    token = _RESOLVE_CACHE.set({} if live is None else live)  # an EMPTY live memo is still live
    try:
        if resolver is not None:
            return _smart(await _walk(plan, root, resolver, None))
        async with Resolver() as rs:  # clean entry: no hand-built fetcher/request
            return _smart(await _walk(plan, root, rs, None))
    finally:
        _RESOLVE_CACHE.reset(token)


async def run_blob(blob: str, root: object = None, *, resolver: "Resolver | None" = None) -> object:
    """Service/remote dispatch, server side: rebuild a plan from its blob (names validated -- the
    wire safety boundary) and run it locally."""
    return await arun(Plan.from_blob(blob).validate_names(), root, resolver=resolver)


async def _walk(
    plan: Plan, root: object, rs: "Resolver", row: "dict[str, object] | None"
) -> object:
    """Walk a plan's steps against a context, returning the RAW current value (Field/Collection/…);
    the smart unwrap happens only at the :func:`arun` terminal. ``row`` is the element's
    extracted-so-far row, so ``field(name)`` inside a sub-expression reads an earlier column.
    """
    cur = await _root_value(plan, root, rs)
    steps, i = plan.steps, 0
    while i < len(steps):
        s = steps[i]
        if s.kind == "get":
            nxt = steps[i + 1] if i + 1 < len(steps) else None
            if nxt is not None and nxt.kind == "call":
                cur = await _invoke(cur, s.name, nxt, root, rs, row)
                i += 2
            else:  # a bare attribute access = a property read
                cur = await _invoke(cur, s.name, None, root, rs, row)
                i += 1
        elif s.kind == "op":
            other = await _arg(s.args[0], root, rs, row) if s.args else _MISSING
            cur = _apply_op(cur, s.name, other)
            i += 1
        elif s.kind == "fn":
            cur = _apply_fn(cur, s.name)
            i += 1
        else:  # when
            cur = await _when(s, root, rs, row)
            i += 1
    return cur


async def _root_value(plan: Plan, root: object, rs: "Resolver") -> object:
    """The starting value: a ``reference(url)`` root is a :class:`Ref` (a ``resolve`` step fetches
    it; collected bare, it reads as the reference); any other root is the passed context (the
    collect() argument, or the element a sub-expr runs against)."""
    if plan.source is not None:
        return Ref(plan.source, base=plan.source)
    return root


# -- dispatch ----------------------------------------------------------------


async def _invoke(
    cur: object,
    name: str,
    call: "Step | None",
    root: object,
    rs: "Resolver",
    row: "dict[str, object] | None",
) -> object:
    """Dispatch one ``get`` (+ optional ``call``) step. The row-shaping ops and the fetch/lookup ops
    are handled explicitly; everything else dispatches onto the current value (fanning out over a
    Collection)."""
    if name == "doc":  # the reference -> document join spelling (wc.resolve(url).doc()); identity
        return cur
    if name == "resolve":  # the reference -> document fetch join (with optional per-step policy)
        policy = call is not None and any(k in _RESOLVE_POLICY for k in call.kwargs)
        return await _resolve(
            cur,
            _effective_resolver(rs, call),
            optional=_flag(call, "optional"),
            memo=not policy,  # a policy-free resolve is memoised by URL (dedupe detail-page fetches)
        )
    if name == "reference":  # a URL held in an earlier-extracted column
        col = _literal(call)
        return (row or {}).get(str(col)) if row is not None else None
    if name == "field":  # a value already extracted in this element's row
        return Field((row or {}).get(str(_literal(call))))
    if name in _ROW_OPS and isinstance(cur, Collection):
        return await _row_op(cur, name, call, rs)
    if name in _ROW_OPS and isinstance(cur, (Document, Element)):
        # a row-op on ONE document/element -- the fan-out after a per-record `.resolve()`:
        # `select('a').attr('href').resolve().extract(body=..., author=...)` selects INSIDE the
        # resolved page (it is the root of every column), yielding one nested row. Without this
        # the columns were evaluated eagerly against the OUTER row and missed.
        rows = await _row_op(Collection([cur]), name, call, rs)
        if name == "filter":
            return cur if len(rows) else None
        return (rows._rows or [{}])[0]
    args, kwargs = await _eager_args(call, root, rs, row)
    if isinstance(cur, Collection):
        return _fan(cur, name, args, kwargs)
    return _one(cur, name, args, kwargs)


async def _resolve(
    cur: object, rs: "Resolver", *, optional: bool = False, memo: bool = True
) -> object:
    """Resolve the current value to a Document (or a Collection of them). A ``Ref`` / ``Field`` /
    URL string is fetched; a Document passes through; a Collection or list of refs FANS OUT into a
    Collection of Documents (``select_all('a').attr('href').resolve()``). Loud by default: nothing
    to resolve (a prior select/attr missed) raises unless ``optional`` -- then it is ``None``. When
    ``memo`` (a policy-free resolve), a URL fetched earlier in this run is served from the run's memo
    -- so N detail-page fields following one record's link cost ONE fetch, not N.
    """
    if isinstance(cur, (Collection, list)):
        docs: list[object] = []
        for item in cur:
            doc = await _resolve(item, rs, optional=optional, memo=memo)
            if doc is not None:
                docs.append(doc)
        return Collection(docs)
    url = cur.url if isinstance(cur, Ref) else (cur.get() if isinstance(cur, Field) else cur)
    if isinstance(url, str) and url:
        cache = _RESOLVE_CACHE.get() if memo else None
        if cache is not None and url in cache:
            return cache[url]  # already fetched this page in this run -- reuse it
        try:
            doc = await rs.resolve(Request(url=url))
        except WebException:  # a TRANSPORT failure (resolve policy raised); optional tolerates it
            if optional:
                return None
            raise
        if cache is not None:
            cache[url] = doc
        return doc
    if isinstance(cur, Document):
        return cur
    if not optional:
        raise WebException(
            err(
                "dsl.resolve_miss",
                "nothing to resolve -- a prior select / attr matched nothing",
            )
        )
    return None


def _effective_resolver(rs: "Resolver", call: "Step | None") -> "Resolver":
    """The resolver a ``resolve(...)`` step uses: the bound one when the step carries no policy, else
    a per-step Resolver built from its recorded (JSON-safe) policy kwargs -- a named ``profile`` +
    ``paginate`` / ``retry`` / ``rate_limit`` / ``rotate`` / ``raise_on_error`` -- reusing the bound
    resolver's pool (so a browser is still shared, not relaunched)."""
    opts = {k: a.value for k, a in call.kwargs.items()} if call is not None else {}
    if not (opts.keys() & _RESOLVE_POLICY):
        return rs
    name = opts.get("profile")
    pager = opts.get("paginate")
    return Resolver(
        profile=_profiles.get(str(name)) if isinstance(name, str) else None,
        paginate=(
            PaginatePolicy(param=pager, max_pages=int(_num(opts.get("max_pages"), 5)))
            if isinstance(pager, str)
            else None
        ),
        rate=(RatePolicy(per_host=_num(opts["rate_limit"], 0.0)) if "rate_limit" in opts else None),
        retry=(RetryPolicy(max_attempts=int(_num(opts["retry"], 0))) if "retry" in opts else None),
        rotate=RotationPolicy() if opts.get("rotate") else None,
        raise_on_error=(bool(opts["raise_on_error"]) if "raise_on_error" in opts else None),
        pool=rs.pool,
    )


def _num(value: object, default: float) -> float:
    """A number from a JSON literal (int/float/str), or ``default`` when absent/unparseable."""
    if isinstance(value, (int, float, str)):
        try:
            return float(value)
        except ValueError:
            return default
    return default


def _flag(call: "Step | None", key: str) -> bool:
    """A boolean literal keyword of a call (e.g. ``optional=True``), default ``False``."""
    if call is None:
        return False
    arg = call.kwargs.get(key)
    return bool(arg.value) if arg is not None else False


def _one(obj: object, name: str, args: "list[object]", kwargs: "dict[str, object]") -> object:
    """Dispatch a single read onto one value. The SAME verbs work over an HTML and a JSON document:
    ``select`` / ``select_all`` navigate (a CSS selector for markup, a dotted JSON path for JSON),
    ``attr`` / ``text`` read a leaf (an HTML attribute/text, or a JSON scalar). A miss (``None``)
    short-circuits the chain; scalars come back wrapped in a ``Field`` so the read helpers chain.
    """
    if obj is None:
        return None
    if name == "select":
        if _markup(obj):
            el = obj.select(str(args[0])) if isinstance(obj, (Document, Element)) else None
            if el is None and not kwargs.get(
                "optional"
            ):  # loud by default: a miss names the selector
                raise WebException(
                    err(
                        "dsl.select_miss",
                        f"selector {args[0]!r} matched nothing",
                        url=_base_of(obj),
                        selector=str(args[0]),
                    )
                )
            return el
        return _json_get(
            obj, str(args[0])
        )  # a JSON sub-value (dict/list -> navigable, scalar -> leaf)
    if name == "select_all":
        if _markup(obj):
            items = obj.select_all(str(args[0])) if isinstance(obj, (Document, Element)) else []
            return Collection(items, base=_base_of(obj))
        value = _json_get(obj, str(args[0]))  # the JSON array at the path becomes the collection
        nodes: "list[object]" = (
            value if isinstance(value, list) else ([] if value is None else [value])
        )
        return Collection(nodes, base=_base_of(obj))
    if name == "attr":  # HTML attribute (attr('text') -> text, attr('href') -> a resolvable Ref)
        key = str(args[0]) if args else ""
        if isinstance(obj, Element):
            if key in _TEXT_ATTRS:
                return Field(_text_of(obj), base=_base_of(obj))
            if key in _LINK_ATTRS:  # a link -> a Ref, so .resolve() can follow it
                return Ref(obj.attr(key) or "", base=_base_of(obj))
            return Field(obj.attr(key), base=_base_of(obj))
        if _markup(obj):  # a markup Document has no attributes of its own; text pseudo only
            return Field(_text_of(obj) if key in _TEXT_ATTRS else None, base=_base_of(obj))
        return Field(_json_get(obj, "" if key in _TEXT_ATTRS else key))  # JSON: value / key access
    if name == "text":
        if _markup(obj):
            return Field(_text_of(obj), base=_base_of(obj))
        return Field(_json_get(obj, ""))  # the JSON scalar value itself
    if name == "links" and isinstance(obj, Document):  # resolvable refs, so .resolve() can follow
        return Collection([Ref(u, base=obj.url) for u in obj.links()])
    attr = getattr(obj, name, _MISSING)
    if attr is _MISSING:  # a verb this value does not have -- LOUD, never a silent null
        raise WebException(
            err("dsl.unknown_verb", f"{type(obj).__name__} has no verb {name!r}", verb=name)
        )
    value = attr(*args, **kwargs) if callable(attr) else attr
    return _wrap(value, _base_of(obj))


def _markup(obj: object) -> bool:
    """Whether a value is a MARKUP surface (an Element, or a non-JSON Document) -- so ``select`` etc.
    use CSS/xpath; a JSON Document or a plain JSON value (dict/list/scalar) navigates by path.
    """
    return isinstance(obj, Element) or (isinstance(obj, Document) and obj.kind != "json")


def _json_get(obj: object, path: str) -> object:
    """Follow a dotted JSON ``path`` into a value (a JSON Document's parsed value, or a JSON node
    reached earlier), reusing :func:`web.parse.dig`. Returns the raw sub-value (dict/list -> further
    navigable, scalar -> a leaf); ``None`` for a non-JSON input or a miss."""
    node = obj.json() if isinstance(obj, Document) and obj.kind == "json" else obj
    if isinstance(node, (dict, list, str, int, float, bool)) or node is None:
        return dig(cast(JsonValue, node), path)
    return None


def _fan(
    coll: "Collection[object]",
    name: str,
    args: "list[object]",
    kwargs: "dict[str, object]",
) -> object:
    """Fan an element op out over a collection, ALWAYS returning a Collection so the chain stays
    uniform (``select``/``select_all`` flatten nested collections and drop misses; a scalar read
    like ``attr``/``text`` yields a Collection of Fields/Refs -- so ``.text().number()`` chains).
    """
    if name in {"project", "merge", "limit", "distinct", "documents"}:
        return getattr(coll, name)(*args, **kwargs)
    flat: list[object] = []
    for item in coll:
        result = _one(item, name, args, kwargs)
        if isinstance(result, Collection):  # select_all fanned -> flatten one level
            flat.extend(result)
        elif result is None and name in {"select", "select_all"}:
            continue  # a select miss drops out of the collection
        else:
            flat.append(result)
    return coll.derive(flat)


async def _row_op(
    coll: "Collection[object]", name: str, call: "Step | None", rs: "Resolver"
) -> "Collection[object]":
    """``extract`` / ``filter``: evaluate the recorded sub-expressions PER element. ``extract``
    annotates each element's row (a later column can read an earlier one via ``field``); ``filter``
    keeps the elements every predicate is truthy for (predicates see the element's row).
    """
    args = list(call.args) if call is not None else []
    kwargs = dict(call.kwargs) if call is not None else {}
    if name == "filter":
        keep_items: list[object] = []
        keep_rows: list[dict[str, object]] = []
        rows = coll._rows or [{} for _ in coll]
        for item, row in zip(coll, rows):
            keep = True
            for (
                a
            ) in args:  # explicit loop: an `await` inside all(...) would build an async generator
                if not _truthy(await _arg(a, item, rs, row)):
                    keep = False
                    break
            if keep:
                keep_items.append(item)
                keep_rows.append(row)
        return coll.derive(keep_items, keep_rows if coll._rows is not None else None)
    # extract: named columns (kwargs), evaluated in order so a later column can reference an earlier
    out_rows: list[dict[str, object]] = []
    base = coll._rows or [{} for _ in coll]
    for item, prior in zip(coll, base):
        built: dict[str, object] = dict(prior)
        for key, arg in kwargs.items():
            # a column is either a recorded sub-expression (walk it against this element) or a bare
            # CONSTANT (e.g. status="UPCOMING") -- _arg yields the literal as-is. (It used to route
            # through a plan wrapper that compared the element TO the literal, so a constant column
            # came back as False.)
            built[key] = raw(await _arg(arg, item, rs, built))
        out_rows.append(built)
    return coll.derive(list(coll), out_rows)


# -- operators / functions / branches ---------------------------------------


def _apply_op(cur: object, name: str, other: object) -> bool:
    """Fold a comparison / boolean op to a plain ``bool`` (operands unwrapped to their raw values)."""
    a, b = _val(cur), _val(other)
    if name == "not":
        return not _truthy(cur)
    if name == "and":
        return _truthy(cur) and _truthy(other)
    if name == "or":
        return _truthy(cur) or _truthy(other)
    if name == "eq":
        return bool(a == b)
    if name == "ne":
        return bool(a != b)
    return _order(a, b, name)


def _order(a: object, b: object, name: str) -> bool:
    """An ordering comparison (``lt`` / ``le`` / ``gt`` / ``ge``) over two like, orderable operands
    (numbers or strings); incomparable operands (a miss, mixed types) never match an ordering.
    """
    if (
        isinstance(a, (int, float))
        and not isinstance(a, bool)
        and isinstance(b, (int, float))
        and not isinstance(b, bool)
    ):
        an, bn = float(a), float(b)
        return {"lt": an < bn, "le": an <= bn, "gt": an > bn, "ge": an >= bn}[name]
    if isinstance(a, str) and isinstance(b, str):
        return {"lt": a < b, "le": a <= b, "gt": a > b, "ge": a >= b}[name]
    return False


def _apply_fn(cur: object, name: str) -> "Field[bool]":
    """The free-function forms ``is_ok`` / ``is_empty`` -- fold the current value to a boolean Field."""
    field = cur if isinstance(cur, Field) else Field(cur)
    return field.is_ok() if name == "is_ok" else field.is_empty()


async def _when(
    step: "Step", root: object, rs: "Resolver", row: "dict[str, object] | None"
) -> object:
    """A ``when(cond).then(a).otherwise(b)`` branch: evaluate the three sub-expressions against the
    current element and take ``then`` when the condition is truthy, else ``otherwise``.
    """
    cond, then, other = (step.args + [Arg(), Arg(), Arg()])[:3]
    if _truthy(await _arg(cond, root, rs, row)):
        return await _arg(then, root, rs, row)
    return await _arg(other, root, rs, row)


# -- helpers -----------------------------------------------------------------


async def _eager_args(
    call: "Step | None", root: object, rs: "Resolver", row: "dict[str, object] | None"
) -> "tuple[list[object], dict[str, object]]":
    """Evaluate a call's args/kwargs eagerly (a literal as-is, a sub-plan walked once) -- for the
    ordinary ops whose arguments are not per-element sub-expressions."""
    if call is None:
        return [], {}
    args = [await _arg(a, root, rs, row) for a in call.args]
    kwargs = {k: await _arg(v, root, rs, row) for k, v in call.kwargs.items()}
    return args, kwargs


async def _arg(arg: "Arg", root: object, rs: "Resolver", row: "dict[str, object] | None") -> object:
    """One argument value: its literal, or the result of walking its sub-plan against the context."""
    if arg.plan is not None:
        return await _walk(arg.plan, root, rs, row)
    return arg.value


def _literal(call: "Step | None") -> "JsonValue":
    """The first positional literal of a call (``reference("link")`` / ``field("name")``)."""
    if call is None or not call.args:
        return None
    return call.args[0].value


def _text_of(obj: object) -> str:
    """The text of a Document/Element (both expose a ``text`` property)."""
    return obj.text if isinstance(obj, (Document, Element)) else str(obj)


def _base_of(obj: object) -> str:
    """The base URL a value was read on -- an Element's origin or a Document's URL."""
    if isinstance(obj, Element):
        return obj._base
    if isinstance(obj, Document):
        return obj.url
    return ""


def _wrap(value: object, base: str) -> object:
    """Wrap a dispatched scalar as a ``Field`` (so the read helpers chain); leave surfaces, lists
    and dicts as they are."""
    if isinstance(value, (Document, Element, Collection, Field, list, dict)):
        return value
    return Field(value, base=base)


def _val(v: object) -> object:
    """The comparable raw value behind a ``Field`` (else the value itself)."""
    return v.get() if isinstance(v, Field) else v


def _truthy(v: object) -> bool:
    """Truthiness for a predicate/condition: a ``Field`` by its value, ``None`` false, else ``bool``."""
    if isinstance(v, Field):
        return bool(v)
    return bool(v)


def _smart(cur: object) -> object:
    """The terminal shape: a ``Field`` yields its value; a Collection with extracted rows yields the
    projected ``list[dict]`` (the implicit project -- no explicit ``.project()`` needed), else its
    items smart-unwrapped; a list maps through; a Document/Element/scalar passes as-is.
    """
    if isinstance(cur, Field):
        return cur.get()
    if isinstance(cur, Collection):
        return cur.project() if cur._rows is not None else [_smart(i) for i in cur]
    if isinstance(cur, list):
        return [_smart(i) for i in cur]
    return cur  # a Ref reads as itself (a live reference); a Document/Element/scalar passes through


__all__ = ["arun", "run_blob"]
