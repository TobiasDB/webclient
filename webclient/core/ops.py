"""Op dispatch (PLAN §9): the surface objects (Reference/Document/Collection/
Field) are data; their *behaviour* lives here as registered ops, and the
executor runs a plan step by dispatching through this module rather than
calling a method on the value. Each op is the same ``@policy``-wrapped body
the method had -- ``self`` is the value the executor is walking.

Ops register against a receiver class name and ``run_op`` resolves by walking
the value's MRO, so a ``WebBase`` op serves every subclass while a
``Collection`` override wins for a collection. Migration stays green: an op
not yet registered for a value falls back to a method on it.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable, Sequence, cast

from .base import (CLASSES, RETURN, Collection, ErrorPolicy, Field, OpError,
                   _extracted, _gather, _plain, default_policy, policy)

if TYPE_CHECKING:
    from ..events import (ActionEvent, ConsoleEvent, DOMUpdateEvent, Event,
                          Topic, XHREvent)
    from .base import Collection, WebBase
    from .document import Document, Reference

#: receiver-class-name -> {op-name -> callable(self, *args, **kwargs)}
CALL_OPS: dict[str, dict[str, Callable[..., Any]]] = {}
#: receiver-class-name -> {attr-name -> fn(self)} for computed bare reads
PROP_OPS: dict[str, dict[str, Callable[..., Any]]] = {}


def op(receiver: str, name: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Register a called op ``name`` on ``receiver`` (decorate the
    @policy-wrapped function; ``self`` = the value)."""
    def register(fn: Callable[..., Any]) -> Callable[..., Any]:
        CALL_OPS.setdefault(receiver, {})[name] = fn
        return fn
    return register


def prop(receiver: str, name: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Register a computed bare-read accessor ``name`` on ``receiver``."""
    def register(fn: Callable[..., Any]) -> Callable[..., Any]:
        PROP_OPS.setdefault(receiver, {})[name] = fn
        return fn
    return register


def _lookup(table: dict[str, dict[str, Any]], value: Any, name: str) -> Any:
    for klass in type(value).__mro__:
        ops = table.get(klass.__name__)
        if ops is not None and name in ops:
            return ops[name]
    return None


def run_op(value: Any, name: str, args: list[Any], kwargs: dict[str, Any]) -> Any:
    """Dispatch a called op, resolving by the value's MRO; fall back to a
    method for an op not yet migrated."""
    fn = _lookup(CALL_OPS, value, name)
    if fn is not None:
        return fn(value, *args, **kwargs)
    return getattr(value, name)(*args, **kwargs)


def read_prop(value: Any, name: str) -> Any:
    """Read a bare attribute: a registered computed accessor, else the value's
    own attribute (a plain data field, or a not-yet-migrated property)."""
    fn = _lookup(PROP_OPS, value, name)
    if fn is not None:
        return fn(value)
    return getattr(value, name)


def core_of(doc: Any) -> Any:
    """The ``DocumentCore`` for a document -- lazily created and cached on the
    document's ``_core_obj`` slot. The runtime/op machinery lives on the core,
    not on the (data-only) Document (PLAN §9)."""
    if doc._core_obj is None:
        from .document import DocumentCore
        doc._core_obj = DocumentCore(doc)
    return doc._core_obj


async def _aextract(obj: Any, named_expr: dict[str, Any]) -> Any:
    """Evaluate each expression against ``obj`` and store it under its name;
    later names see earlier ones. A missing field is None, not fatal."""
    from .executor import evaluate
    with default_policy(RETURN):
        for name, expr in named_expr.items():
            obj._fields[name] = await evaluate(expr, obj)
    return obj


async def _aextract_all(coll: Any, named_expr: dict[str, Any]) -> Any:
    """Extract across every element of a collection."""
    for el in coll._items:
        await run_op(el, "extract", [], named_expr)
    return coll


async def _afilter(coll: Any, exprs: tuple[Any, ...]) -> Any:
    """Keep the elements for which every expression is truthy (a not-ok result
    is falsy)."""
    from .executor import _truthy, evaluate
    kept: list[Any] = []
    with default_policy(RETURN):
        for el in coll._items:
            results = [await evaluate(e, el) for e in exprs]
            if all(_truthy(r) for r in results):
                kept.append(el)
    out: Any = Collection()
    out._items = kept
    return out


def _is_empty_of(obj: Any) -> bool:
    """Per-kind emptiness (was ``_is_empty`` on the data classes): a Field is
    empty when its value is falsy-ish, a Collection when it has no elements, a
    Document when it has no content, anything else when it has no fields."""
    from .document import Document
    if isinstance(obj, Field):
        return obj.value is None or obj.value in ("", [], {}, b"")
    if isinstance(obj, Collection):
        return not obj._items
    if isinstance(obj, Document):
        return not obj.content
    return not obj._fields


# ========================================================================= #
# Document ops -- delegate to the DocumentCore / backings.
# ========================================================================= #

@op("Document", "select")
@policy(returns="Document")
def select(self: Document, selector: str, *, index: int = 0,
           wait: float | None = None, error: ErrorPolicy | None = None,
           optional: bool = False) -> Document:
    return core_of(self).dispatch("select", selector, index=index, wait=wait)


@op("Document", "select_all")
@policy(returns="Collection")
def select_all(self: Document, selector: str, limit: int | None = None,
               offset: int = 0, *, error: ErrorPolicy | None = None
               ) -> "Collection[Document]":
    return core_of(self).dispatch("select_all", selector, limit=limit, offset=offset)


@op("Document", "attr")
@policy(returns="Field")
def attr(self: Document, name: str, *,
         error: ErrorPolicy | None = None) -> "Field[str] | Reference":
    return core_of(self).dispatch("attr", name)


@op("Document", "render")
@policy(returns="None")
def render(self: Document, format: str, **options: Any) -> Any:
    return core_of(self).dispatch("render", format, **options)


@op("Document", "click")
@policy(returns="Self")
def click(self: Document, selector: str | None = None, *,
          error: ErrorPolicy | None = None, **kw: Any) -> Document:
    return core_of(self).dispatch("click", selector, **kw)


@op("Document", "write")
@policy(returns="Self")
def write(self: Document, selector: str, text: str, *,
          error: ErrorPolicy | None = None, **kw: Any) -> Document:
    return core_of(self).dispatch("write", selector, text, **kw)


@op("Document", "press")
@policy(returns="Self")
def press(self: Document, key: str, *,
          error: ErrorPolicy | None = None, **kw: Any) -> Document:
    return core_of(self).dispatch("press", key, **kw)


@op("Document", "hover")
@policy(returns="Self")
def hover(self: Document, selector: str, *,
          error: ErrorPolicy | None = None, **kw: Any) -> Document:
    return core_of(self).dispatch("hover", selector, **kw)


@op("Document", "check")
@policy(returns="Self")
def check(self: Document, selector: str, checked: bool = True, *,
          error: ErrorPolicy | None = None, **kw: Any) -> Document:
    return core_of(self).dispatch("check", selector, checked, **kw)


@op("Document", "select_option")
@policy(returns="Self")
def select_option(self: Document, selector: str, *,
                  error: ErrorPolicy | None = None, **kw: Any) -> Document:
    return core_of(self).dispatch("select_option", selector, **kw)


@op("Document", "upload")
@policy(returns="Self")
def upload(self: Document, selector: str, files: Sequence[str], *,
           error: ErrorPolicy | None = None) -> Document:
    return core_of(self).dispatch("upload", selector, files)


@op("Document", "drag")
@policy(returns="Self")
def drag(self: Document, source: str, target: str, *,
         error: ErrorPolicy | None = None) -> Document:
    return core_of(self).dispatch("drag", source, target)


@op("Document", "scroll")
@policy(returns="Self")
def scroll(self: Document, selector: str | None = None, *, x: int = 0,
           y: int = 0, error: ErrorPolicy | None = None) -> Document:
    return core_of(self).dispatch("scroll", selector, x=x, y=y)


@op("Document", "execute")
@policy(returns="Self")
def execute(self: Document, script: str, *,
            error: ErrorPolicy | None = None) -> Document:
    return core_of(self).dispatch("execute", script)


@op("Document", "evaluate")
@policy(returns="Field")
def evaluate(self: Document, script: str, *,
             error: ErrorPolicy | None = None) -> Any:
    return core_of(self).dispatch("evaluate", script)


@op("Document", "screenshot")
@policy(returns="Document")
def screenshot(self: Document, selector: str | None = None, *,
               error: ErrorPolicy | None = None, **kw: Any) -> Document:
    return core_of(self).dispatch("screenshot", selector, **kw)


@op("Document", "wait_for")
@policy(returns="Self")
def wait_for(self: Document, selector: str | None = None, *,
             error: ErrorPolicy | None = None, **kw: Any) -> Document:
    return core_of(self).dispatch("wait_for", selector, **kw)


@prop("Document", "title")
def title(self: Document) -> str | None:
    node = run_op(self, "select", ["title"], {"error": RETURN})
    return node.text if node.ok else None


@prop("Document", "text")
def text(self: Document) -> str:
    return core_of(self).text()


@prop("Document", "identity_path")
def identity_path(self: Document) -> str | None:
    return core_of(self).identity_path()


@op("Document", "join")
def document_join(self: Document, href: str) -> Reference:
    return core_of(self).join(href)


@op("Document", "ref")
def ref(self: Document) -> Reference:
    return core_of(self).ref()


@op("Document", "reload")
def reload(self: Document, **options: Any) -> Document:
    return core_of(self).reload(**options)


# -- event views (the EventBacking; consolidated off Document, PLAN §9) -------

@op("Document", "events_of")
def events_of(self: Document, event: "type[Event] | Topic") -> "Sequence[Event]":
    return core_of(self).dispatch("events_of", event)


@op("Document", "subscribe")
def subscribe(self: Document, topic: "Topic", handler: Any) -> Any:
    return core_of(self).dispatch("subscribe", topic, handler)


@prop("Document", "action_events")
def action_events(self: Document) -> "Sequence[ActionEvent]":
    return core_of(self).dispatch("action_events")


@prop("Document", "xhr_requests")
def xhr_requests(self: Document) -> "Sequence[XHREvent]":
    return core_of(self).dispatch("xhr_requests")


@prop("Document", "dom_mutations")
def dom_mutations(self: Document) -> "Sequence[DOMUpdateEvent]":
    return core_of(self).dispatch("dom_mutations")


@prop("Document", "console")
def console(self: Document) -> "Sequence[ConsoleEvent]":
    return core_of(self).dispatch("console")


# ========================================================================= #
# Reference ops -- resolve (via the bound client) + pure derivations.
# ========================================================================= #

@op("Reference", "resolve")
@policy(returns="Document", require="ok")
def resolve(self: Reference, *, browser: bool = False, session: Any = None,
            optional: bool = False, error: ErrorPolicy | None = None,
            **options: Any) -> Document:
    from .document import request_fields
    wc = self._client or (self._session._client if self._session else None)
    if wc is None:
        from .expr import Expr, Plan
        root = Expr(Plan(root="Reference", source=request_fields(self)))
        return cast("Document", run_op(root, "resolve", [],
                    {"browser": browser, "optional": optional, **options}))
    return wc.resolve(self, browser=browser, optional=optional,
                      session=session or self._session, **options)


@op("Reference", "replace")
def replace(self: Reference, **fields: Any) -> Reference:
    return self.model_copy(update={**fields, "name": "",
                                   "root": self.name or self.root})


@op("Reference", "with_params")
def with_params(self: Reference, **params: str) -> Reference:
    return cast("Reference", run_op(
        self, "replace", [], {"params": {**self.params, **params}}))


@op("Reference", "join")
def join(self: Reference, href: str) -> Reference:
    from urllib.parse import urljoin

    from .document import from_url, url_of
    return from_url(urljoin(url_of(self), href))


@prop("Reference", "url")
def url(self: Reference) -> str:
    from .document import url_of
    return url_of(self)


# ========================================================================= #
# WebBase ops -- extraction + accessors (serve every subclass via the MRO).
# ========================================================================= #

@op("WebBase", "extract")
@policy(returns="Self")
def extract(self: WebBase, *, error: ErrorPolicy | None = None,
            **named_expr: Any) -> WebBase:
    return _aextract(self, named_expr)


@op("WebBase", "project")
def project(self: WebBase, model: Any = None, *,
            error: ErrorPolicy | None = None) -> Any:
    if not self.ok:
        raise OpError(self.error)
    data = {k: _plain(v) for k, v in self._fields.items()}
    return model(**data) if model is not None else data


@op("WebBase", "is_empty")
@policy(returns="Field", always=True)
def is_empty(self: WebBase, *, error: ErrorPolicy | None = None) -> "Field[bool]":
    return Field[bool](value=_is_empty_of(self))


@op("WebBase", "is_ok")
@policy(returns="Field", always=True)
def is_ok(self: WebBase, *, error: ErrorPolicy | None = None) -> "Field[bool]":
    return Field[bool](value=self.ok)


@op("WebBase", "field")
@policy(returns="Field")
def field(self: WebBase, name: str, *,
          error: ErrorPolicy | None = None) -> "Field[Any]":
    value = _extracted(self, name)
    return value if isinstance(value, Field) else Field[Any](value=value)


@op("WebBase", "reference")
@policy(returns="Reference")
def reference(self: WebBase, name: str, *,
              error: ErrorPolicy | None = None) -> Reference:
    return cast("Reference", _extracted(self, name, CLASSES["Reference"]))


@op("WebBase", "references")
@policy(returns="Collection")
def references(self: WebBase, *names: str,
               error: ErrorPolicy | None = None) -> "Collection[Reference]":
    return _gather(self, names, CLASSES["Reference"])


@op("WebBase", "document")
@policy(returns="Document")
def document(self: WebBase, name: str, *,
             error: ErrorPolicy | None = None) -> Document:
    return cast("Document", _extracted(self, name, CLASSES["Document"]))


@op("WebBase", "documents")
@policy(returns="Collection")
def documents(self: WebBase, *names: str,
              error: ErrorPolicy | None = None) -> "Collection[Document]":
    return _gather(self, names, CLASSES["Document"])


# ========================================================================= #
# Collection ops -- element-mapped extract/filter/project (whole-collection).
# ========================================================================= #

@op("Collection", "extract")
@policy(returns="Self")
def collection_extract(self: Collection, *, error: ErrorPolicy | None = None,
                       **named_expr: Any) -> Collection:
    return _aextract_all(self, named_expr)


@op("Collection", "filter")
@policy(returns="Collection")
def collection_filter(self: Collection, *exprs: Any,
                      error: ErrorPolicy | None = None,
                      **named_expr: Any) -> Collection:
    return _afilter(self, (*exprs, *named_expr.values()))


@op("Collection", "project")
def collection_project(self: Collection, model: Any = None, *,
                       error: ErrorPolicy | None = None) -> list[Any]:
    return [run_op(el, "project", [model], {}) for el in self._items]


__all__ = ["CALL_OPS", "PROP_OPS", "op", "prop", "run_op", "read_prop"]
