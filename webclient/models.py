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

from typing import TYPE_CHECKING, Any, Generic, Literal, TypeVar, overload

if TYPE_CHECKING:
    from .collection import Collection, Field
    from .core.document_core import Element
    from .surfaces import Document, Reference

T = TypeVar("T")
S = TypeVar("S")


class Lazy(Generic[T]):
    """Bridge: a recorded plan that materialises to ``T``."""

    def collect(self, context: Any = ...) -> T: ...


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
    ok: "LazyField[bool]"
    url: "LazyField[str]"
    def join(self, href: str) -> "LazyReference": ...
    def replace(self, **fields: Any) -> "LazyReference": ...
    def resolve(self, *, browser: bool = ..., optional: bool = ..., error: Any = ...) -> "LazyDocument": ...
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
    action_events: "list[Any]"
    console: "list[Any]"
    dom_mutations: "list[Any]"
    events: "list[Any]"
    message: "LazyField[str]"
    ok: "LazyField[bool]"
    text: "LazyField[str]"
    title: "LazyField[str]"
    @overload
    def attr(self, name: Literal['href', 'src', 'action']) -> "LazyReference": ...  # type: ignore[overload-overlap]
    @overload
    def attr(self, name: str, *, error: Any = ...) -> "LazyField[str]": ...
    def click(self, selector: str | None = ..., *, timeout: float | None = ..., optional: bool = ...) -> "LazyDocument": ...
    def evaluate(self, script: str) -> "LazyField[Any]": ...
    def events_of(self, event_type: Any) -> "list[Any]": ...
    def is_empty(self) -> "LazyField[bool]": ...
    def is_ok(self) -> "LazyField[bool]": ...
    def ref(self) -> "LazyReference": ...
    def reload(self) -> "LazyDocument": ...
    @overload
    def render(self, format: Literal['elements']) -> "list[Element]": ...
    @overload
    def render(self, format: Literal['links']) -> "LazyCollection[LazyReference]": ...
    @overload
    def render(self, format: str, **options: Any) -> "LazyField[str]": ...
    def screenshot(self, selector: str | None = ...) -> "LazyDocument": ...
    def select(self, selector: str, *, index: int = ..., error: Any = ...) -> "LazyDocument": ...
    def select_all(self, selector: str, *, limit: int | None = ..., offset: int = ...) -> "LazyCollection[LazyDocument]": ...
    def summary(self) -> "LazyField[dict[str, Any]]": ...
    def wait_for(self, selector: str | None = ..., *, timeout: float | None = ...) -> "LazyDocument": ...
    def write(self, selector: str, text: str, *, timeout: float | None = ..., optional: bool = ...) -> "LazyDocument": ...
    def field(self, name: str) -> "LazyField[Any]": ...
    def reference(self, name: str) -> "LazyReference": ...
    def collect(self, context: Any = ...) -> "Document": ...


class LazyCollection(Lazy["Collection[T]"], Generic[T]):
    def extract(self, **exprs: Any) -> "LazyCollection[T]": ...
    def filter(self, *predicates: Any) -> "LazyCollection[T]": ...
    def limit(self, n: int) -> "LazyCollection[T]": ...
    def documents(self, column: str) -> "LazyCollection[LazyDocument]": ...
    def project(self) -> "Lazy[list[dict[str, Any]]]": ...
    def collect(self, context: Any = ...) -> "Collection[T]": ...
# fmt: on
# >>> end generated <<<


__all__ = ["Lazy", "LazyField", "LazyReference", "LazyDocument", "LazyCollection"]
