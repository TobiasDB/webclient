"""Op dispatch (PLAN §9): the surface objects (Reference/Document/Collection/
Field) are data; their *behaviour* lives here as registered ops, and the
executor runs a plan step by dispatching through this module rather than
calling a method on the value. Each op is wrapped by the same ``@policy``
envelope the methods used, so error/capability semantics are unchanged.

Migration is incremental and green: an op not yet moved off its class falls
back to the method on the value. Once every op is registered, the classes
carry no methods and the fallback is dead.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable, Literal, Sequence

from .base import RETURN, ErrorPolicy, policy

if TYPE_CHECKING:
    from .document import Document, Reference
    from .base import Collection, Field

#: op-name -> callable(value, *args, **kwargs); @policy-wrapped like the methods
CALL_OPS: dict[str, Callable[..., Any]] = {}
#: computed accessors read as a bare attribute (no call): name -> fn(value)
PROP_OPS: dict[str, Callable[..., Any]] = {}


def op(name: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Register ``fn(self, ...)`` as the call-op ``name`` (``self`` = the value
    the executor is walking). Decorate the @policy-wrapped function."""
    def register(fn: Callable[..., Any]) -> Callable[..., Any]:
        CALL_OPS[name] = fn
        return fn
    return register


def prop(name: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Register ``fn(self)`` as the computed accessor ``name`` (a bare read)."""
    def register(fn: Callable[..., Any]) -> Callable[..., Any]:
        PROP_OPS[name] = fn
        return fn
    return register


def run_op(value: Any, name: str, args: list[Any], kwargs: dict[str, Any]) -> Any:
    """Dispatch a called op. Falls back to a method on ``value`` for any op
    not yet migrated into the registry."""
    fn = CALL_OPS.get(name)
    if fn is not None:
        return fn(value, *args, **kwargs)
    return getattr(value, name)(*args, **kwargs)


def read_prop(value: Any, name: str) -> Any:
    """Read a bare attribute: a registered computed accessor, else the value's
    own attribute (a plain data field, or a not-yet-migrated property)."""
    fn = PROP_OPS.get(name)
    if fn is not None:
        return fn(value)
    return getattr(value, name)


# ========================================================================= #
# Document ops (PLAN §9): behaviour moved off the Document class. Each is the
# same @policy-wrapped body the method had -- ``self`` is the Document the
# executor is walking -- delegating to its DocumentCore / the backings.
# ========================================================================= #

@op("select")
@policy(returns="Document")
def select(self: Document, selector: str, *, index: int = 0,
           wait: float | None = None, error: ErrorPolicy | None = None,
           optional: bool = False) -> Document:
    return self._core.dispatch("select", selector, index=index, wait=wait)


@op("select_all")
@policy(returns="Collection")
def select_all(self: Document, selector: str, limit: int | None = None,
               offset: int = 0, *, error: ErrorPolicy | None = None
               ) -> "Collection[Document]":
    return self._core.dispatch("select_all", selector, limit=limit, offset=offset)


@op("attr")
@policy(returns="Field")
def attr(self: Document, name: str, *,
         error: ErrorPolicy | None = None) -> "Field[str] | Reference":
    return self._core.dispatch("attr", name)


@op("render")
@policy(returns="None")
def render(self: Document, format: str, **options: Any) -> Any:
    return self._core.dispatch("render", format, **options)


@op("click")
@policy(returns="Self")
def click(self: Document, selector: str | None = None, *,
          error: ErrorPolicy | None = None, **kw: Any) -> Document:
    return self._core.dispatch("click", selector, **kw)


@op("write")
@policy(returns="Self")
def write(self: Document, selector: str, text: str, *,
          error: ErrorPolicy | None = None, **kw: Any) -> Document:
    return self._core.dispatch("write", selector, text, **kw)


@op("press")
@policy(returns="Self")
def press(self: Document, key: str, *,
          error: ErrorPolicy | None = None, **kw: Any) -> Document:
    return self._core.dispatch("press", key, **kw)


@op("hover")
@policy(returns="Self")
def hover(self: Document, selector: str, *,
          error: ErrorPolicy | None = None, **kw: Any) -> Document:
    return self._core.dispatch("hover", selector, **kw)


@op("check")
@policy(returns="Self")
def check(self: Document, selector: str, checked: bool = True, *,
          error: ErrorPolicy | None = None, **kw: Any) -> Document:
    return self._core.dispatch("check", selector, checked, **kw)


@op("select_option")
@policy(returns="Self")
def select_option(self: Document, selector: str, *,
                  error: ErrorPolicy | None = None, **kw: Any) -> Document:
    return self._core.dispatch("select_option", selector, **kw)


@op("upload")
@policy(returns="Self")
def upload(self: Document, selector: str, files: Sequence[str], *,
           error: ErrorPolicy | None = None) -> Document:
    return self._core.dispatch("upload", selector, files)


@op("drag")
@policy(returns="Self")
def drag(self: Document, source: str, target: str, *,
         error: ErrorPolicy | None = None) -> Document:
    return self._core.dispatch("drag", source, target)


@op("scroll")
@policy(returns="Self")
def scroll(self: Document, selector: str | None = None, *, x: int = 0,
           y: int = 0, error: ErrorPolicy | None = None) -> Document:
    return self._core.dispatch("scroll", selector, x=x, y=y)


@op("execute")
@policy(returns="Self")
def execute(self: Document, script: str, *,
            error: ErrorPolicy | None = None) -> Document:
    return self._core.dispatch("execute", script)


@op("evaluate")
@policy(returns="Field")
def evaluate(self: Document, script: str, *,
             error: ErrorPolicy | None = None) -> Any:
    return self._core.dispatch("evaluate", script)


@op("screenshot")
@policy(returns="Document")
def screenshot(self: Document, selector: str | None = None, *,
               error: ErrorPolicy | None = None, **kw: Any) -> Document:
    return self._core.dispatch("screenshot", selector, **kw)


@op("wait_for")
@policy(returns="Self")
def wait_for(self: Document, selector: str | None = None, *,
             error: ErrorPolicy | None = None, **kw: Any) -> Document:
    return self._core.dispatch("wait_for", selector, **kw)


@prop("title")
def title(self: Document) -> str | None:
    node = run_op(self, "select", ["title"], {"error": RETURN})
    return node.text if node.ok else None


__all__ = ["CALL_OPS", "PROP_OPS", "op", "prop", "run_op", "read_prop"]
