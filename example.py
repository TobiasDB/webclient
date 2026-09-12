"""example.py — a DESIGN SKETCH of the proposed *full-lazy* surface.

NOT wired to the real engine. Run it: `python example.py`. It exists so we can
read how the core looks if every surface method only builds an expression and a
single core evaluates it.

Key invariant (kept from today): the recorder ``Expr`` is a *generic* wrapper,
independent of the models. It records ANY public attribute/call into a plan --
it has no ``select``/``attr``/``resolve`` methods of its own. The model method
*names* live only on typing shims (``Document``/``Reference``/``Session``,
``Protocol``s -- effectively the ``.pyi``); at runtime the object is always a
generic ``Expr``.

Three addressable lazy roots, symmetric:

    document                document.select(...)        unbound / context root
    document(id)            document("doc-1").render()  address a held doc by id
    reference(url, **args)  reference("https://…")       construct a reference
    reference(id)           reference("r-1")             address a stored ref
    session / session(id)   session("s-1").ref(...)      session, bare or by id

A resolved document is just an ``Expr`` rooted at ``doc:<id>`` -- the only
metadata it needs is the id (decision 4); ``.title`` etc. are lazy ops.

Layering (your model):

    Doc --(expr)--> WC --(expr)--> WCC.execute --(call)--> DocC --(call)--> Backing

The shims only *build* an expr; ``collect()`` is the single edge into
``WCC.execute`` (``Core.evaluate`` here), which turns the expr into real calls.

Decisions baked in: (1) everything is a generic ``Expr``; (2) ``Field`` kept
only as an opt-in ``.field()`` envelope; (3) eager reuses ``.collect()`` -- one
path; (4) the core holds live pages under stable ids.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Literal, NamedTuple, Protocol, overload


# ===========================================================================
# Plan (the wire format a core evaluates)
# ===========================================================================

@dataclass(frozen=True)
class Step:
    kind: str                   # "get" | "call" | "eq" | "gt" | "lt" | "and" | "not" | "when"
    args: tuple = ()
    kwargs: tuple = ()


@dataclass(frozen=True)
class Plan:
    root: str                   # "doc:<id>" | "ref:<url>" | "ref@<id>" | "session:<id>" | "ctx"
    steps: tuple = ()

    def then(self, step: Step) -> "Plan":
        return replace(self, steps=self.steps + (step,))

    def describe(self) -> str:
        out, i, steps = self.root, 0, list(self.steps)
        while i < len(steps):
            s = steps[i]
            if s.kind == "get" and i + 1 < len(steps) and steps[i + 1].kind == "call":
                a = ", ".join(repr(x) for x in steps[i + 1].args)
                out, i = f"{out}.{s.args[0]}({a})", i + 2
            elif s.kind == "get":
                out, i = f"{out}.{s.args[0]}", i + 1
            else:
                out, i = f"({out} {s.kind} …)", i + 1
        return out


# ===========================================================================
# Field — kept reluctantly (decision 2): opt-in value + ok/error envelope
# ===========================================================================

class Field(NamedTuple):
    value: Any
    ok: bool = True
    error: str | None = None


# ===========================================================================
# Expr — the generic recorder. Independent of the models: records any
# attribute/call; knows nothing about select/attr/resolve/etc.
# ===========================================================================

class Expr:
    __slots__ = ("_core", "_plan", "_eager")

    def __init__(self, core: "Core | None", plan: Plan, eager: bool = False):
        self._core, self._plan, self._eager = core, plan, eager

    def _extend(self, step: Step) -> "Expr":
        return Expr(self._core, self._plan.then(step), self._eager)

    # -- generic recording ---------------------------------------------------
    def __getattr__(self, name: str) -> "Expr":
        if name.startswith("_"):
            raise AttributeError(name)
        return self._extend(Step("get", (name,)))

    def __call__(self, *args: Any, _collect: bool | None = None, **kwargs: Any) -> Any:
        nxt = self._extend(Step("call", args, tuple(kwargs.items())))
        eager = self._eager if _collect is None else _collect
        return nxt.collect() if eager else nxt

    # -- recordable operators (predicates for filter / when) -----------------
    def __eq__(self, o: Any) -> "Expr": return self._extend(Step("eq", (o,)))    # type: ignore[override]
    def __gt__(self, o: Any) -> "Expr": return self._extend(Step("gt", (o,)))
    def __lt__(self, o: Any) -> "Expr": return self._extend(Step("lt", (o,)))
    def __and__(self, o: Any) -> "Expr": return self._extend(Step("and", (o,)))
    def __invert__(self) -> "Expr": return self._extend(Step("not"))
    __hash__ = None                                         # type: ignore[assignment]

    # -- the single evaluation trigger ---------------------------------------
    def collect(self) -> Any:
        if self._core is None:
            raise TypeError("a context expr is evaluated inside extract/filter/"
                            "when, not collected on its own")
        return self._core.evaluate(self._plan)

    def field(self) -> Field:
        try:
            return Field(self.collect(), ok=True)
        except Exception as exc:                            # noqa: BLE001
            return Field(None, ok=False, error=str(exc))

    @property
    def id(self) -> str | None:
        """The stable handle address (structural); the only metadata a resolved
        document needs. Present when rooted at a concrete id."""
        r = self._plan.root
        for prefix in ("doc:", "ref@", "session:"):
            if r.startswith(prefix):
                return r[len(prefix):]
        return None

    def __iter__(self):
        out = self.collect()
        return iter(out if isinstance(out, list) else [out])

    def __repr__(self) -> str:
        return f"<lazy {self._plan.describe()}>"


# ===========================================================================
# Typing shims (the ".pyi"): method NAMES live here, not on Expr. Runtime is
# always Expr; these only give the checker a fluent, typed surface.
# ===========================================================================

# The two-tier shim (your proposal). The LAZY tier records ops and types
# `collect()` to the MATERIALIZED tier; the materialized tier adds the response
# data and keeps the lazy methods (so chaining off a collected document builds
# new lazy exprs again). At runtime every lazy tier is one generic `Expr`;
# collect() returns a real materialized object. lazy(cls) picks the tier.
_LINK = Literal["href", "src", "action"]


class LazyScalar(Protocol):
    def collect(self) -> Any: ...
    def field(self) -> Field: ...
    def resolve(self) -> "LazyDocument": ...                # when the value is a url
    def __eq__(self, o: Any) -> "LazyBool": ...             # type: ignore[override]
    def __gt__(self, o: Any) -> "LazyBool": ...
    def __lt__(self, o: Any) -> "LazyBool": ...


class LazyBool(Protocol):
    def __and__(self, o: "LazyBool") -> "LazyBool": ...
    def __invert__(self) -> "LazyBool": ...
    def collect(self) -> bool: ...


class LazyReference(Protocol):
    def resolve(self) -> "LazyDocument": ...
    def with_params(self, **params: str) -> "LazyReference": ...
    def collect(self) -> "Reference": ...


class LazyDocument(Protocol):
    id: str
    @property
    def title(self) -> LazyScalar: ...
    # DECISION: eager is per-call `_collect=True`, typed by an overload that
    # returns the MATERIALIZED tier directly (verified: pyright + mypy resolve
    # it). No separate Eager type family. These `_collect` overload pairs are
    # mechanical and repeat on every value op, so gen_stubs.py emits them (the
    # same generator that already emits the Collection twin).
    @overload
    def select(self, selector: str, *, _collect: Literal[True], index: int = 0) -> "Document": ...
    @overload
    def select(self, selector: str, *, index: int = 0) -> "LazyDocument": ...
    @overload
    def select_all(self, selector: str, *, _collect: Literal[True]) -> list["Document"]: ...
    @overload
    def select_all(self, selector: str) -> "LazyCollection[LazyDocument, Document]": ...
    @overload
    def attr(self, name: _LINK) -> LazyReference: ...       # link attrs -> a reference
    @overload
    def attr(self, name: str, *, _collect: Literal[True]) -> Any: ...
    @overload
    def attr(self, name: str) -> LazyScalar: ...
    @overload
    def render(self, format: str = "markdown", *, _collect: Literal[True]) -> str: ...
    @overload
    def render(self, format: str = "markdown") -> LazyScalar: ...
    def extract(self, **exprs: Any) -> "LazyDocument": ...
    def click(self, selector: str) -> "LazyDocument": ...
    def write(self, selector: str, text: str) -> "LazyDocument": ...
    def collect(self) -> "Document": ...                    # lazy -> materialized


class Document(LazyDocument, Protocol):
    """Materialized: the response data, plus every lazy method (chaining off it
    records new lazy exprs rooted at this document's id). ``collect()`` is
    identity. Note ``title`` here is a real value, which *overrides* the lazy
    ``title -> LazyScalar`` -- that override is the same tension as eager mode
    (a materialized tier returns values where the lazy tier returns exprs)."""
    status_code: int
    content: bytes
    url: str
    kind: str
    title: str                                              # type: ignore[assignment]
    def collect(self) -> "Document": ...


class LazyCollection[LazyT, T](Protocol):
    """A lazy collection: parametrised by its element's lazy type (chain) and
    its collected type (``collect() -> list[T]``) -- your Collection[LazyT, T]."""
    def select(self, selector: str, *, index: int = 0) -> "LazyCollection[LazyT, T]": ...
    def select_all(self, selector: str) -> "LazyCollection[LazyT, T]": ...
    def attr(self, name: str) -> "LazyCollection[LazyScalar, Any]": ...
    def extract(self, **exprs: Any) -> "LazyCollection[LazyT, T]": ...
    def filter(self, predicate: LazyBool) -> "LazyCollection[LazyT, T]": ...
    def project(self) -> list[dict[str, Any]]: ...
    def collect(self) -> list[T]: ...


class Reference(Protocol):                                  # materialized reference
    id: str
    url: str


class Session(Protocol):
    id: str
    def ref(self, url: str) -> LazyReference: ...
    def document(self, doc_id: str) -> LazyDocument: ...
    def fetch(self, url: str) -> LazyDocument: ...


# Callable root shims: bare is the lazy type (document.select(...)); calling
# addresses by id (reference also constructs from a url).
class _DocumentRoot(LazyDocument, Protocol):
    def __call__(self, doc_id: str = ...) -> LazyDocument: ...


class _ReferenceRoot(LazyReference, Protocol):
    def __call__(self, url_or_id: str = ..., **kwargs: Any) -> LazyReference: ...
 

class _SessionRoot(Session, Protocol):
    def __call__(self, sid: str = ...) -> Session: ...


# ===========================================================================
# Addressable lazy roots: symmetric bare / by-id / (reference) construct.
# A root is both attribute-accessible (bare -> a context expr) and callable
# (by id, or for `reference` construct from a url). Runtime: a generic Expr.
# ===========================================================================

class _Root:
    __slots__ = ("_tag", "_core")

    def __init__(self, tag: str, core: "Core | None"):
        self._tag, self._core = tag, core

    def __getattr__(self, name: str) -> Expr:              # bare: document.select(...)
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(Expr(self._core, Plan("ctx")), name)

    def __call__(self, arg: Any = None, /, **kwargs: Any) -> Expr:
        if self._tag == "ref":
            if kwargs or (isinstance(arg, str) and "://" in arg):     # construct
                return Expr(self._core, Plan(f"ref:{arg or kwargs.get('url', '')}"))
            if arg is not None:                                       # address by id
                return Expr(self._core, Plan(f"ref@{arg}"))
            return Expr(self._core, Plan("ctx"))
        if arg is None:                                               # bare
            return Expr(self._core, Plan("ctx"))
        return Expr(self._core, Plan(f"{self._tag}:{arg}"))           # doc:id / session:id

    def __repr__(self) -> str:
        return f"<root {self._tag}>"


class when:
    """Polars-style branching, not a Field method: when(c).then(a).otherwise(b)."""

    def __init__(self, cond: Any):
        self._cond = cond

    def then(self, value: Any) -> "when":
        self._then = value
        return self

    def otherwise(self, value: Any) -> Expr:
        return Expr(None, Plan("ctx").then(Step("when", (self._cond, self._then, value))))


def filter(collection: Any, predicate: Any) -> Any:
    """Free-function form (your ``wc.filter``); == ``collection.filter(pred)``."""
    return collection.filter(predicate)


# ===========================================================================
# The shims: WebClient / WebSession. They only build Expr roots.
# ===========================================================================

class WebClient:
    def __init__(self, core: "Core", *, eager: bool = False):
        self._core, self._eager = core, eager
        core._eager_docs = eager

    def reference(self, url: str, **kwargs: Any) -> Reference:
        return _Root("ref", self._core)(url, **kwargs)

    def document(self, doc_id: str) -> Document:
        return _Root("doc", self._core)(doc_id)

    def fetch(self, url: str, *, _collect: bool | None = None) -> Document:
        q = Expr(self._core, Plan(f"ref:{url}")).resolve()       # reference(url).resolve()
        eager = self._eager if _collect is None else _collect
        return q.collect() if eager else q

    def execute(self, expr: Any) -> Any:
        return expr.collect()

    def session(self, **opts: Any) -> "WebSession":
        return WebSession(self._core, self._core.open_session(**opts), self._eager)


class WebSession:
    def __init__(self, core: "Core", sid: str, eager: bool):
        self._core, self.id, self._eager = core, sid, eager

    def fetch(self, url: str) -> Document:
        q = Expr(self._core, Plan(f"ref:{url}|{self.id}")).resolve()
        return q.collect() if self._eager else q


# ===========================================================================
# Mock core — the only thing that evaluates a plan (WCC.execute + DocC + Backing).
# ===========================================================================

def _el(**kw: Any) -> dict:
    return {"attrs": {}, "sel": {}, **kw}


_SHOP = _el(
    id="doc-1", kind="html", title="Demo Shop",
    render={"markdown": "# Featured\n\n- Aeropress\n- Grinder",
            "text": "Featured coffee gear.", "links": ["/i/1", "/i/2"]},
    sel={".card": [
        _el(attrs={"price": 39},
            sel={".title": "Aeropress", "a": _el(attrs={"href": "/i/1", "text": "go"})}),
        _el(attrs={"price": 129},
            sel={".title": "Grinder", "a": _el(attrs={"href": "/i/2", "text": "go"})}),
    ]},
)
_ITEMS = {
    "/i/1": _el(id="doc-2", kind="json", fields={"name": "Aeropress", "stock": 7}),
    "/i/2": _el(id="doc-3", kind="json", fields={"name": "Grinder", "stock": 0}),
}
_WEB = {"https://shop.test/": _SHOP}


def _truthy(v: Any) -> bool:
    return bool(v)


def _clone(node: dict) -> dict:
    import copy
    return copy.deepcopy(node)


class _Document:
    """Runtime MATERIALIZED document (typed as ``Document``): the response data,
    plus lazy op-building delegated to an Expr rooted at its id -- so chaining
    off it is lazy again. ``collect()`` is identity."""

    __slots__ = ("_core", "id", "url", "kind", "status_code", "content", "_title")

    def __init__(self, core: "Core", node: dict):
        self._core = core
        self.id = node["id"]
        self.url = node.get("url", "")
        self.kind = node.get("kind", "html")
        self.status_code = node.get("status_code", 200)
        self.content = node.get("content", b"")
        self._title = node.get("title")

    @property
    def title(self) -> Any:                         # materialized: a real value
        return self._title

    def collect(self) -> "_Document":
        return self

    def _q(self) -> Expr:
        return Expr(self._core, Plan(f"doc:{self.id}"), self._core._eager_docs)

    def __getattr__(self, name: str) -> Any:        # select/select_all/attr/render/… -> lazy
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self._q(), name)

    def __repr__(self) -> str:
        return f"Document(id={self.id!r}, status={self.status_code}, title={self._title!r})"


class Core:
    label = "local"

    def __init__(self) -> None:
        self._live: dict[str, dict] = {}
        self._eager_docs = False

    def open_session(self, **opts: Any) -> str:
        return "sess-1"

    def evaluate(self, plan: Plan) -> Any:               # the one expr->calls edge
        value = self._start(plan.root)
        value = self._run(value, _ops(plan.steps), ctx=value)
        return self._wrap(value)

    def _start(self, root: str) -> Any:
        base = root.split("|", 1)[0]
        if base.startswith("doc:"):
            return self._live[base[len("doc:"):]]        # the held live page
        if base.startswith("ref:"):
            return {"_kind": "ref", "_url": base[len("ref:"):]}
        if base.startswith("session:"):
            return {"_kind": "session", "_id": base[len("session:"):]}
        if base.startswith("ref@"):
            return {"_kind": "ref", "_url": ""}          # stored ref (not modelled here)
        raise TypeError("a context expr needs a surrounding extract/filter")

    def _run(self, value: Any, ops: list, ctx: Any) -> Any:
        for j, (name, args, kw, is_op) in enumerate(ops):
            if isinstance(value, list) and not is_op and name not in ("filter", "project"):
                return [self._run(v, ops[j:], v) for v in value]     # fan-out
            value = self._apply(value, name, args, kw, is_op, ctx)
        return value

    def _sub(self, ctx: Any, expr: Expr) -> Any:
        return self._run(ctx, _ops(expr._plan.steps), ctx)

    def _apply(self, value, name, args, kw, is_op, ctx):
        if is_op:
            return self._operator(name, value, args, ctx)
        if name == "resolve":                            # reference/link -> document
            url = value["_url"] if isinstance(value, dict) and value.get("_kind") == "ref" else value
            doc = _clone(_WEB.get(url) or _ITEMS[url])
            self._live[doc["id"]] = doc
            return doc
        if name == "ref":                                # on a session -> a reference
            return {"_kind": "ref", "_url": args[0]}
        if name == "select_all":
            return list(value.get("sel", {}).get(args[0], []))
        if name == "select":
            picked = value.get("sel", {}).get(args[0])
            if picked is None and "fields" in value:
                picked = value["fields"].get(args[0])
            if picked is None:
                raise LookupError(f"no match for {args[0]!r}")
            if isinstance(picked, list):
                picked = picked[kw.get("index", 0)]
            return picked if isinstance(picked, dict) else _el(attrs={"text": picked})
        if name in ("attr", "__attr_get__"):
            return self._read(value, args[0])
        if name == "render":
            return value["render"][args[0]]
        if name == "extract":
            return {k: (self._sub(ctx, v) if isinstance(v, Expr) else v)
                    for k, v in kw.items()}
        if name == "project":
            return value
        if name == "filter":
            return [v for v in value if _truthy(self._sub(v, args[0]))]
        if name == "click":
            value["sel"][".flash"] = "clicked " + args[0]
            return value
        if name == "write":
            value["sel"]["#out"] = args[1]
            return value
        raise ValueError(f"unknown op {name!r}")

    def _operator(self, name, value, args, ctx):
        if name == "not":
            return not _truthy(value)
        if name == "and":
            return _truthy(value) and _truthy(self._sub(ctx, args[0]))
        if name == "when":
            cond, then_v, else_v = args
            chosen = then_v if _truthy(self._sub(ctx, cond)) else else_v
            return self._sub(ctx, chosen) if isinstance(chosen, Expr) else chosen
        other = args[0]
        if name == "eq":
            return value == other
        if name == "gt":
            return float(value) > float(other)
        if name == "lt":
            return float(value) < float(other)

    def _read(self, value, name):
        if isinstance(value, dict):
            if "fields" in value and name in value["fields"]:
                return value["fields"][name]
            if name in value.get("attrs", {}):
                return value["attrs"][name]
            if name in value:                            # doc meta: title / kind
                return value[name]
        return None

    def _wrap(self, value):
        if isinstance(value, dict) and "id" in value:            # a document ->
            return _Document(self, value)                        # the materialized form
        return value


class RemoteCore(Core):
    label = "remote"

    def evaluate(self, plan: Plan) -> Any:
        print(f"    · POST /execute  plan={plan.describe()[:46]}…")
        return super().evaluate(plan)


def _ops(steps: tuple) -> list:
    """Parse recorded steps into ops: pair a 'get' with a following 'call' into
    a method call; a lone 'get' is an attribute read; operators pass through.
    This is exactly how the real executor dispatches a generic Expr by name."""
    out, i, steps = [], 0, list(steps)
    while i < len(steps):
        s = steps[i]
        if s.kind == "get":
            name = s.args[0]
            if i + 1 < len(steps) and steps[i + 1].kind == "call":
                c = steps[i + 1]
                out.append((name, c.args, dict(c.kwargs), False))
                i += 2
            else:
                out.append(("__attr_get__", (name,), {}, False))
                i += 1
        else:
            out.append((s.kind, s.args, {}, True))
            i += 1
    return out


# -- module-level default backend + the addressable roots --------------------
_DEFAULT = Core()
document: _DocumentRoot = _Root("doc", _DEFAULT)         # type: ignore[assignment]
reference: _ReferenceRoot = _Root("ref", _DEFAULT)      # type: ignore[assignment]
session: _SessionRoot = _Root("session", _DEFAULT)      # type: ignore[assignment]


# ===========================================================================
# Interactions
# ===========================================================================

def main() -> None:
    wc = WebClient(Core())
    URL = "https://shop.test/"

    print("== lazy by default ==")
    q = wc.fetch(URL).render("markdown")
    print("  built, nothing ran:", q)
    print("  collect:", wc.execute(q).splitlines()[0])

    print("\n== collect() materializes: LazyDocument -> Document (data + still chainable) ==")
    page = wc.fetch(URL).collect()               # Document: has response data
    print("  materialized:", page, "| status:", page.status_code, "| title:", page.title)

    print("\n== selection + attributes (generic recording; lazy until collect) ==")
    print("  one:   ", page.select(".card").select(".title").attr("text").collect())
    print("  many:  ", page.select_all(".card").select(".title").attr("text").collect())

    print("\n== extract + project: the lazy element-map (document = per-element ctx) ==")
    rows = (page.select_all(".card")
            .extract(title=document.select(".title").attr("text"),
                     price=document.attr("price"),
                     link=document.select("a").attr("href"))
            .project().collect())
    print("  rows:  ", rows)

    print("\n== branching (when/then/otherwise) + follow a link ==")
    enriched = (page.select_all(".card")
                .extract(name=document.select("a").attr("href").resolve()
                         .select("name").attr("text"),
                         status=when(document.select("a").attr("href").resolve().attr("stock") > 0)
                         .then("in stock").otherwise("sold out"))
                .project().collect())
    print("  rows:  ", enriched)

    print("\n== filter: free function (your wc.filter) or recorded .filter ==")
    pricey = (filter(page.select_all(".card"), document.attr("price") > 50)
              .extract(title=document.select(".title").attr("text"),
                       price=document.attr("price")).project().collect())
    print("  kept:  ", pricey)

    print("\n== comprehension is EAGER: iterating a query collects it now ==")
    for row in (page.select_all(".card")
                .extract(title=document.select(".title").attr("text")).project()):
        print("  row:   ", row)

    print("\n== Field, opt-in (decision 2) ==")
    print("  field: ", page.select(".card").attr("price").field())
    print("  miss:  ", page.select(".nope").attr("text").field())

    print("\n== eager mode: same code, collected immediately (reuses .collect) ==")
    eager = WebClient(Core(), eager=True)
    print("  render already a string:", eager.fetch(URL).render("markdown").splitlines()[0])
    print("  select_all already a list of", len(eager.fetch(URL).select_all(".card")), "elements")
    print("  _collect=True on one op:", wc.fetch(URL).render("text", _collect=True))

    print("\n== sessions hold the live page; interactions mutate it by id ==")
    s = wc.session()
    live = s.fetch(URL).collect()
    live.click(".card button").collect()
    live.write("#search", "aeropress").collect()
    print("  live id:", live.id, "-> re-read after interaction:")
    print("  flash: ", live.select(".flash").attr("text").collect())
    print("  out:   ", wc.document(live.id).select("#out").attr("text").collect())

    print("\n== addressable roots: reference / document / session (bare or by id) ==")
    ref_expr = reference(URL)                            # construct from a url
    print("  reference(url):", ref_expr)
    page2 = ref_expr.resolve().collect()                # -> a document (default core)
    print("  reference(url).resolve().id:", page2.id)
    print("  document(id).title:", document(page2.id).title.collect())     # address by id
    print("  session(id):", session("sess-1"), "| bare roots:", reference, document, session)
    print("  session(id).ref(url).resolve().title:",
          session("sess-1").ref(URL).resolve().title.collect())

    print("\n== remote is the SAME code over a different core (interchangeable) ==")
    rc = WebClient(RemoteCore())
    remote_rows = (rc.fetch(URL).select_all(".card")
                   .extract(title=document.select(".title").attr("text"))
                   .project().collect())
    print("  remote rows:", remote_rows)


if __name__ == "__main__":
    main()
