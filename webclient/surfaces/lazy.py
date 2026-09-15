"""The lazy typed tier -- static stubs for the recording surface.

At runtime every recording value is an ``Expr`` (see ``webclient.expr``); these
classes exist only for the type checker. A lazy op returns another lazy type;
``collect()`` (via the ``Lazy[T]`` bridge) and ``WebClient.execute`` materialise
it to the eager model.

The classes below (between the ``>>> generated <<<`` markers) are produced by
``scripts/gen_stubs.py`` -- the same ``members`` walk that emits the eager
surfaces, in the lazy vocabulary -- from each core's fields + its backings' typed
ops, and gated by ``--check``. ``LazyField`` / ``LazyCollection`` are the value/
iterable leaves (hand-written, like ``Field`` / ``Collection``).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from typing import TYPE_CHECKING, Any, Generic, Literal, TypeVar, cast, overload

from ..query.expr import Expr, to_arg
from ..query.plan import Plan, Step

if TYPE_CHECKING:
    from ..collection import Collection, Field
    from ..core.document import Element
    from ..core.client.models import SearchResult
    from ..models import ActionEvent, ConsoleEvent, DOMUpdateEvent, Event
    from ..summary import Metadata, Probe, Runtime, Structure, Summary, Transport
    from .eager import Document, Reference

T = TypeVar("T")
S = TypeVar("S")
E = TypeVar("E", bound="Event")  # an event subtype, for events_of(cls) -> list[cls]


class Lazy(Generic[T]):
    """Bridge: a recorded plan realised by one path -- ``collect``/``acollect``
    (materialise to ``T``) or ``stream``/``astream`` (iterate rows). Every lazy
    value is an ``Expr`` at runtime; ``_plan`` is its recorded ``Plan`` (the wire
    form) and ``_client`` its bound client, both exposed here for introspection
    (``expr._plan.describe()`` / ``.model_dump()``)."""

    _plan: "Plan"
    _client: Any

    def collect(self, context: Any = ...) -> T: ...
    async def acollect(self, context: Any = ...) -> T: ...
    def stream(self, context: Any = ...) -> "Iterator[Any]": ...
    def astream(self, context: Any = ...) -> "AsyncIterator[Any]": ...
    def to_blob(self) -> str: ...  # the whole chain as a compact, rebuildable blob
    def explain(self) -> str: ...  # a readable one-line rendering of the chain


# >>> generated: lazy-tier <<<
# fmt: off
class LazyField(Lazy["Field[S]"], Generic[S]):
    def get(self, default: Any = ...) -> S: ...
    def is_ok(self) -> "LazyField[bool]": ...
    def is_empty(self) -> "LazyField[bool]": ...
    def __eq__(self, o: Any) -> "LazyField[bool]": ...  # type: ignore[override]
    def __ne__(self, o: Any) -> "LazyField[bool]": ...  # type: ignore[override]
    def __and__(self, o: Any) -> "LazyField[bool]": ...
    def __or__(self, o: Any) -> "LazyField[bool]": ...
    def __invert__(self) -> "LazyField[bool]": ...
    def collect(self, context: Any = ...) -> "Field[S]": ...


class LazyReference(Lazy["Reference"]):
    kind: LazyField[str]
    name: LazyField[str]
    root: LazyField[str]
    hostname: LazyField[str]
    method: LazyField[str]
    scheme: LazyField[str]
    port: LazyField[int | None]
    path: LazyField[str]
    fragment: LazyField[str]
    params: LazyField[Any]
    headers: LazyField[Any]
    cookies: LazyField[Any]
    body: LazyField[bytes | None]
    json_body: LazyField[Any]
    form: LazyField[Any]
    follow_redirects: LazyField[bool]
    timeout: LazyField[float | None]
    actions: LazyField[Any]
    expect: LazyField[Any]
    ok: "LazyField[bool]"
    url: "LazyField[str]"
    def join(self, href: str) -> "LazyReference": ...
    def replace(self, **fields: Any) -> "LazyReference": ...
    def resolve(self, *, browser: "bool | Literal['never', 'auto', 'always', 'probe']" = ..., optional: bool = ..., error: Any = ...) -> "LazyDocument": ...
    def with_params(self, **params: str) -> "LazyReference": ...
    def collect(self, context: Any = ...) -> "Reference": ...


class LazyDocument(Lazy["Document"]):
    id: LazyField[str]
    name: LazyField[str]
    root: LazyField[str]
    session_id: LazyField[str]
    kind: LazyField[str]
    url: LazyField[str]
    final_url: LazyField[str | None]
    content: LazyField[bytes]
    status_code: LazyField[int]
    response_headers: LazyField[Any]
    encoding: LazyField[str | None]
    elapsed: LazyField[float | None]
    created: LazyField[float]
    accessed: LazyField[float]
    error: LazyField[Any]
    action_events: "list[ActionEvent]"
    console: "list[ConsoleEvent]"
    dom_mutations: "list[DOMUpdateEvent]"
    events: "list[Event]"
    message: "LazyField[str]"
    ok: "LazyField[bool]"
    text_content: "LazyField[str]"
    title: "LazyField[str]"
    @overload
    def attr(self, name: Literal['href', 'src', 'action']) -> "LazyReference": ...  # type: ignore[overload-overlap]
    @overload
    def attr(self, name: str, *, optional: bool = ..., error: Any = ...) -> "LazyField[str]": ...
    def click(self, selector: str | None = ..., *, timeout: float | None = ..., optional: bool = ..., error: Any = ...) -> "LazyDocument": ...
    def elements(self) -> "list[Element]": ...
    def evaluate(self, script: str) -> "LazyField[Any]": ...
    @overload
    def events_of(self, event_type: type[E]) -> "list[E]": ...
    @overload
    def events_of(self, event_type: str) -> "list[Event]": ...
    def html(self) -> "LazyField[str]": ...
    def is_empty(self) -> "LazyField[bool]": ...
    def is_ok(self) -> "LazyField[bool]": ...
    def links(self) -> "LazyCollection[LazyReference]": ...
    def markdown(self, *, main_content_only: bool = ...) -> "LazyField[str]": ...
    def metadata(self) -> "Lazy[Metadata]": ...
    def probe(self) -> "Lazy[Probe]": ...
    def ref(self) -> "LazyReference": ...
    def reload(self) -> "LazyDocument": ...
    @overload
    def render(self, format: Literal['elements']) -> "list[Element]": ...
    @overload
    def render(self, format: Literal['links']) -> "LazyCollection[LazyReference]": ...
    @overload
    def render(self, format: str, **options: Any) -> "LazyField[str]": ...
    def runtime(self) -> "Lazy[Runtime]": ...
    def screenshot(self, selector: str | None = ...) -> "LazyDocument": ...
    def select(self, selector: str, *, index: int = ..., optional: bool = ..., error: Any = ...) -> "LazyDocument": ...
    def select_all(self, selector: str, *, limit: int | None = ..., offset: int = ...) -> "LazyCollection[LazyDocument]": ...
    def skeleton(self, *, max_lines: int = ..., text_chars: int = ..., max_depth: int = ..., max_siblings: int = ..., legend: bool = ..., annotate_origin: bool = ...) -> "LazyField[str]": ...
    def structure(self) -> "Lazy[Structure]": ...
    def summary(self, *include: str, exclude: Any = ...) -> "Lazy[Summary]": ...
    def text(self, *, main_content_only: bool = ...) -> "LazyField[str]": ...
    def transport(self) -> "Lazy[Transport]": ...
    def wait_for(self, selector: str | None = ..., *, timeout: float | None = ..., optional: bool = ..., error: Any = ...) -> "LazyDocument": ...
    def write(self, selector: str, text: str, *, timeout: float | None = ..., optional: bool = ..., error: Any = ...) -> "LazyDocument": ...
    def field(self, name: str) -> "LazyField[Any]": ...
    def reference(self, name: str) -> "LazyReference": ...
    def collect(self, context: Any = ...) -> "Document": ...


class LazyCollection(Lazy["Collection[T]"], Generic[T]):
    def attr(self, name: str, *, optional: bool = ..., error: Any = ...) -> "LazyCollection[LazyField[str]]": ...
    def click(self, selector: str | None = ..., *, timeout: float | None = ..., optional: bool = ..., error: Any = ...) -> "LazyCollection[LazyDocument]": ...
    def evaluate(self, script: str) -> "LazyCollection[LazyField[Any]]": ...
    def html(self) -> "LazyCollection[LazyField[str]]": ...
    def is_empty(self) -> "LazyCollection[LazyField[bool]]": ...
    def is_ok(self) -> "LazyCollection[LazyField[bool]]": ...
    def links(self) -> "LazyCollection[LazyReference]": ...
    def markdown(self, *, main_content_only: bool = ...) -> "LazyCollection[LazyField[str]]": ...
    message: "LazyCollection[LazyField[str]]"
    def ref(self) -> "LazyCollection[LazyReference]": ...
    def reload(self) -> "LazyCollection[LazyDocument]": ...
    def render(self, format: str, **options: Any) -> "LazyCollection[LazyField[str]]": ...
    def screenshot(self, selector: str | None = ...) -> "LazyCollection[LazyDocument]": ...
    def select(self, selector: str, *, index: int = ..., optional: bool = ..., error: Any = ...) -> "LazyCollection[LazyDocument]": ...
    def select_all(self, selector: str, *, limit: int | None = ..., offset: int = ...) -> "LazyCollection[LazyDocument]": ...
    def skeleton(self, *, max_lines: int = ..., text_chars: int = ..., max_depth: int = ..., max_siblings: int = ..., legend: bool = ..., annotate_origin: bool = ...) -> "LazyCollection[LazyField[str]]": ...
    def text(self, *, main_content_only: bool = ...) -> "LazyCollection[LazyField[str]]": ...
    text_content: "LazyCollection[LazyField[str]]"
    title: "LazyCollection[LazyField[str]]"
    def wait_for(self, selector: str | None = ..., *, timeout: float | None = ..., optional: bool = ..., error: Any = ...) -> "LazyCollection[LazyDocument]": ...
    def write(self, selector: str, text: str, *, timeout: float | None = ..., optional: bool = ..., error: Any = ...) -> "LazyCollection[LazyDocument]": ...
    def extract(self, **exprs: Any) -> "LazyCollection[T]": ...
    def filter(self, *predicates: Any) -> "LazyCollection[T]": ...
    def limit(self, n: int) -> "LazyCollection[T]": ...
    def documents(self, column: str) -> "LazyCollection[LazyDocument]": ...
    def project(self) -> "Lazy[list[dict[str, Any]]]": ...
    def collect(self, context: Any = ...) -> "Collection[T]": ...


class LazyWebClient:
    """The lazy recorder rooted at a client/session (``wc.lazy``): its verbs
    record a ``WebClient``-rooted plan to batch/defer -- ``wc.lazy.fetch(url)
    .collect()`` records then runs on the client's engine."""
    def discover_sitemaps(self, url: Any, *, limit: int = ...) -> "LazyCollection[LazyReference]": ...
    def fetch(self, url: Any, *, browser: "bool | Literal['never', 'auto', 'always', 'probe']" = ..., optional: bool = ..., error: Any = ..., **kw: Any) -> "LazyDocument": ...
    def ref(self, url: Any, method: str = ..., **kw: Any) -> "LazyReference": ...
    def search(self, query: str, *, limit: int = ..., endpoint: str | None = ..., optional: bool = ..., error: Any = ...) -> "Lazy[list[SearchResult]]": ...
    def summary(self, url: Any, *include: str, **kw: Any) -> "Lazy[Summary]": ...
# fmt: on
# >>> end generated <<<


# --------------------------------------------------------------------------- #
# Lazy authoring layer -- the roots + free builders (surface-facing, so they
# live with the lazy tier rather than in the Expr recorder engine).
# --------------------------------------------------------------------------- #

_MISSING: Any = object()


def reference(url: str, **kwargs: Any) -> "Reference":
    """A lazy reference root starting from ``url``: an ``Expr`` recording a plan
    rooted at that request spec (statically a ``Reference``)."""
    from ..core.reference import from_url

    spec = from_url(url, **kwargs).model_dump()
    return cast("Reference", Expr(Plan(root="Reference", source=spec)))


def field(name: str) -> Any:
    """A value already extracted in the surrounding row/context."""
    return cast(Any, doc).field(name)


def _fn(name: str, expr: Any) -> Expr:
    base = expr if isinstance(expr, Expr) else Expr(Plan())
    return base._extend(Step(kind="fn", name=name))


def is_empty(expr: Any) -> "LazyField[bool]":
    """Free-function form of ``x.is_empty()`` (records an ``fn`` step)."""
    return cast("LazyField[bool]", _fn("is_empty", expr))


def is_ok(expr: Any) -> "LazyField[bool]":
    """Free-function form of ``x.is_ok()`` (records an ``fn`` step)."""
    return cast("LazyField[bool]", _fn("is_ok", expr))


class _When:
    """Polars-style branching builder: ``when(cond).then(a).otherwise(b)`` -- a
    free construct recording a single ``when`` step whose parts are
    sub-expressions evaluated against the surrounding context."""

    __slots__ = ("_cond", "_then")

    def __init__(self, cond: Any) -> None:
        self._cond = cond
        self._then: Any = _MISSING

    def then(self, value: Any) -> "_When":
        self._then = value
        return self

    def otherwise(self, value: Any) -> Any:
        if self._then is _MISSING:
            raise TypeError("when(...).then(...) before .otherwise(...)")
        step = Step(
            kind="when", args=[to_arg(self._cond), to_arg(self._then), to_arg(value)]
        )
        return Expr(Plan(steps=[step]))


def when(cond: Any) -> _When:
    """Start a Polars-style conditional: ``when(cond).then(a).otherwise(b)``."""
    return _When(cond)


def filter(collection: "LazyCollection[T]", *predicates: Any) -> "LazyCollection[T]":
    """Free-function form of the collection filter: ``filter(coll, pred)`` =
    ``coll.filter(pred)`` -- keeps the elements every predicate is truthy for."""
    return collection.filter(*predicates)


#: the lazy roots -- an ``Expr`` rooted at each surface (statically the surface
#: it authors plans for; at runtime an ``Expr``).
if TYPE_CHECKING:
    from ..collection import Collection

    doc: "Document"
    ref: "Reference"
    many: "Collection[Document]"
else:
    doc = Expr(Plan(root="Document"))
    ref = Expr(Plan(root="Reference"))
    many = Expr(Plan(root="Collection"))


class WebQuery:
    """The lazy authoring namespace (``from webclient import wq``). ``wq.doc`` /
    ``wq.ref`` / ``wq.many`` are the lazy roots -- an ``Expr`` recording a plan,
    statically the *lazy* surface (``LazyDocument`` / ``LazyReference`` /
    ``LazyCollection``) so ``.collect()`` / ``.stream()`` / ``._plan`` and the
    recorder-only ``.field()`` / ``.reference()`` are all visible to the type
    checker. ``wq.reference(url)`` roots a plan at a URL; ``wq.when`` /
    ``wq.field`` / ``wq.filter`` are the free builders. Namespacing them under
    ``wq`` keeps the roots from shadowing locals named ``doc`` / ``ref`` /
    ``many``."""

    if TYPE_CHECKING:
        doc: "LazyDocument"
        ref: "LazyReference"
        many: "LazyCollection[LazyDocument]"

        def reference(self, url: str, **kwargs: Any) -> "LazyReference": ...
        def field(self, name: str) -> "LazyField[Any]": ...

    else:
        doc = doc
        ref = ref
        many = many
        reference = staticmethod(reference)
        field = staticmethod(field)

    when = staticmethod(when)
    filter = staticmethod(filter)
    is_ok = staticmethod(is_ok)
    is_empty = staticmethod(is_empty)


#: the singleton lazy-authoring namespace.
wq = WebQuery()


__all__ = [
    "Lazy",
    "LazyField",
    "LazyReference",
    "LazyDocument",
    "LazyCollection",
    "LazyWebClient",
    "reference",
    "field",
    "when",
    "filter",
    "is_empty",
    "is_ok",
    "doc",
    "ref",
    "many",
    "wq",
    "WebQuery",
]
