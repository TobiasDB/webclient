"""`Document` and `Element` — one type each, composed from surfaces.

A Document is one response. Its capabilities come from its backing, at call
time: navigating a page away leaves the old Document's static half working
and its live half raising `StaleDocument`, so capability is runtime state
rather than a fact fixed at resolve time.

An Element is an *address* into a document, with its resolved handle cached
by the backing. That is what lets an Element be serialised, cross a plan
boundary, and mean the same thing over a tree and over a live page.
"""
from __future__ import annotations

from typing import Any, Iterator, Mapping
from uuid import uuid4

from typing_extensions import Self

from .backing import Backing, Kind, StaticBacking
from .errors import StaleDocument, UnsupportedOperation, WebClientError
from .ops import CoreView, bind_ops, op, op_property
from .plan import Plan, Source
from .reference import Reference
from .surfaces import (DataSurface, DomSurface, InteractSurface,
                       RenderSurface, SelectSurface)
from .values import Expr, ExprState, Value, register_lazy_type

_RELEASED = "\x00released"


@bind_ops
class FieldSurface:
    """`field(name)` — a value already extracted in this context. In a plan
    that is the record being built; on a Document it is `doc.fields`."""

    # `Any`: a column's type is a run-time fact, not a static one. Saying
    # otherwise would be a lie that blocks `field("link").resolve()`.
    @op(pure=True, returns="Value", cardinality="one->one")
    def field(self, name: str, *, optional: bool = False) -> Any:
        store = getattr(self, "_fields", {})
        if name not in store:
            if optional:
                return Value(None)
            known = ", ".join(sorted(store)) or "none yet"
            raise WebClientError(
                f"no extracted field {name!r} on this document (have: {known})")
        return Value(store[name])


# --------------------------------------------------------------------------- #
# Element
# --------------------------------------------------------------------------- #

@bind_ops
class Element(SelectSurface, InteractSurface, DomSurface, FieldSurface, Expr):
    """One element of a document, addressed rather than pointed at."""

    def __init__(self, document: "Document | None" = None,
                 path: str | None = None) -> None:
        self._document = document
        self._path = path
        self._expr: ExprState | None = None

    def _init_lazy(self) -> None:
        self._document = None
        self._path = None

    # -- plumbing ------------------------------------------------------------
    @property
    def _backing(self) -> Any:                       # type: ignore[override]
        if self._document is None:
            raise WebClientError("this Element is not bound to a document")
        return self._document._backing

    @property
    def _capabilities(self) -> frozenset[str]:
        return self._document._capabilities if self._document else frozenset()

    @property
    def _backing_name(self) -> str:
        return self._document._backing_name if self._document else "none"

    def _capability_error(self, spec: Any) -> BaseException:
        return self._document._capability_error(spec)   # type: ignore[union-attr]

    def _spawn_element(self, path: str) -> "Element":
        return Element(self._document, path)

    def _link_base(self) -> Reference:
        return self._document._link_base()           # type: ignore[union-attr]

    def _describe(self) -> str:
        return f"element {self._path}"

    @property
    def path(self) -> str | None:
        """This element's address within its document. Serialisable."""
        return self._path

    @property
    def document(self) -> "Document | None":
        return self._document

    @property
    def core(self) -> CoreView:
        return CoreView(self)

    def _eager_value(self) -> Any:
        return self

    def __eq__(self, other: Any) -> Any:             # type: ignore[override]
        if self.is_lazy:
            return self._binop("eq", other)
        return (isinstance(other, Element)
                and other._path == self._path
                and other._document is self._document)

    def __hash__(self) -> int:
        if self.is_lazy:
            raise TypeError("a lazy Element is not hashable")
        return hash((id(self._document), self._path))

    def __repr__(self) -> str:
        if self.is_lazy:
            return f"Element(<lazy {len(self._plan_or_new().steps)} steps>)"
        return f"Element({self._path!r})"


# --------------------------------------------------------------------------- #
# Document
# --------------------------------------------------------------------------- #

@bind_ops
class Document(SelectSurface, DataSurface, RenderSurface, InteractSurface,
               DomSurface, FieldSurface, Expr):
    """A resolved resource."""

    def __init__(self, *, backing: Backing | None = None,
                 request: Reference | None = None, client: Any = None,
                 session: Any = None, id: str | None = None) -> None:
        self._backing_obj: Backing | None = backing
        self.request = request
        self.id = id or uuid4().hex
        self._client = client
        self._session = session
        self._fields: dict[str, Any] = {}
        self._superseded: str | None = None
        self._expr: ExprState | None = None

    def _init_lazy(self) -> None:
        self._backing_obj = None
        self.request = None
        self._client = self._session = None
        self._fields = {}
        self._superseded = None

    # -- construction --------------------------------------------------------
    @classmethod
    def from_content(cls, content: bytes | str, *, kind: Kind = "html",
                     url: str | None = None) -> "Document":
        """A Document over content you already have — no client, no I/O."""
        if isinstance(content, str):
            content = content.encode()
        return cls(backing=StaticBacking(content, kind, final_url=url,
                                         status_code=200),
                   request=Reference.from_url(url) if url else None)

    # -- plumbing ------------------------------------------------------------
    @property
    def _backing(self) -> Any:                       # type: ignore[override]
        if self._backing_obj is None:
            raise WebClientError(
                "this Document has no backing (it is a lazy root)")
        return self._backing_obj

    @property
    def _path(self) -> str | None:                   # type: ignore[override]
        return None

    @property
    def _capabilities(self) -> frozenset[str]:
        if self._backing_obj is None:
            return frozenset()
        return self._backing_obj.capabilities

    @property
    def _backing_name(self) -> str:
        return self._backing_obj.name if self._backing_obj else "none"

    def _capability_error(self, spec: Any) -> BaseException:
        if self._superseded is not None:
            what = ("was released back to the pool"
                    if self._superseded == _RELEASED
                    else f"moved to {self._superseded}")
            return StaleDocument(
                f"{spec.name}() needs this Document's page, which "
                f"{what}. Its content, render and telemetry still work; live "
                "ops belong to the Document that took the page over.")
        return UnsupportedOperation(spec.name, spec.capability or "",
                                    self._backing_name, spec.hint)

    def _spawn_element(self, path: str) -> Element:
        return Element(self, path)

    def _link_base(self) -> Reference:
        """Relative links resolve against the URL that actually answered."""
        target = self._backing_obj.final_url if self._backing_obj else None
        if target:
            return Reference.from_url(target).bind(self._client, self._session)
        if self.request is not None:
            return self.request.bind(self._client, self._session)
        raise WebClientError("this Document has no URL to resolve links against")

    def _renderers(self) -> Any:
        if self._client is not None:
            return self._client.renderers
        from .render import default_registry
        return default_registry()

    async def _freeze(self, moved_to: str | None) -> None:
        """Take the live page's snapshot and drop to a static backing.

        This is what makes a navigated-away Document keep working: it loses
        `browser` from its capabilities and keeps everything static.
        """
        backing = self._backing_obj
        snapshot = getattr(backing, "snapshot", None)
        if snapshot is None:
            return
        self._backing_obj = await snapshot()
        self._superseded = moved_to or _RELEASED

    def _record_extracted(self, record: Any) -> None:
        if isinstance(record, Mapping):
            self._fields.update(record)

    @property
    def core(self) -> CoreView:
        return CoreView(self)

    def supports(self, capability: str) -> bool:
        """Whether this Document can serve ops needing `capability`, right
        now — it can change over the Document's life."""
        return capability in self._capabilities

    @property
    def fields(self) -> dict[str, Any]:
        """Everything extracted from this document so far."""
        return dict(self._fields)

    # -- response facts (recordable in a plan) -------------------------------
    @op_property(returns="Value")
    def status_code(self) -> Value[int]:
        return Value(self._backing.status_code)

    @op_property(returns="Value")
    def ok(self) -> Value[bool]:
        return Value(200 <= self._backing.status_code < 300)

    @op_property(returns="Value")
    def final_url(self) -> Value[str | None]:
        return Value(self._backing.final_url)

    @op_property(returns="Value")
    def kind(self) -> Value[str]:
        return Value(self._backing.kind)

    @op_property(returns="Value")
    def content(self) -> Value[bytes]:
        return Value(self._backing.content)

    @op_property(returns="Value")
    def elapsed(self) -> Value[float | None]:
        return Value(self._backing.elapsed)

    @op_property(returns="Value")
    def message(self) -> Value[str | None]:
        """Why this document is not ok, when it is not."""
        return Value(self._backing.message)

    @op_property(returns="Value")
    def headers(self) -> Value[dict[str, str]]:
        return Value(dict(self._backing.response_headers))

    @property
    def telemetry(self) -> Any:
        from .telemetry import Telemetry
        record = getattr(self._backing, "telemetry", None)
        return record if record is not None else Telemetry()

    # -- lifecycle ops -------------------------------------------------------
    @op(resource="http", returns="Document", cardinality="one->one")
    def reload(self, *, optional: bool = False) -> Any:
        """Resolve this document's request again."""
        if self.request is None:
            raise WebClientError("this Document has no request to reload")
        return self._resolver().resolve(self.request, session=self._session,
                                        optional=optional)

    @op(capability="browser", resource="page", returns="Document",
        cardinality="one->one",
        hint="Re-resolve with wc.resolve(ref, browser=True).")
    def navigate(self, target: "str | Reference", *,
                 wait_until: str = "load") -> Any:
        """Move this page to another URL. Returns the new Document; this one
        keeps its snapshot and loses its live half."""
        reference = (target if isinstance(target, Reference)
                     else self._link_base().join(target))
        return self._client.core.navigate(self, reference,
                                          wait_until=wait_until)

    def _resolver(self) -> Any:
        if self._client is not None:
            return self._client.core
        from .client import default_client
        return default_client().core

    # -- lazy rooting --------------------------------------------------------
    def __call__(self, document_id: str) -> "Document":
        """`doc("id")` — a plan rooted at an already-resolved document."""
        if not self.is_lazy:
            raise WebClientError("only the lazy `doc` root is callable")
        return self._respawn(
            Plan(source=Source(kind="document", document_id=document_id,
                               root="Document")), "Document")

    def _eager_value(self) -> Any:
        return self

    def __eq__(self, other: Any) -> Any:             # type: ignore[override]
        if self.is_lazy:
            return self._binop("eq", other)
        return self is other

    def __hash__(self) -> int:
        if self.is_lazy:
            raise TypeError("a lazy Document is not hashable")
        return id(self)

    def __repr__(self) -> str:
        if self.is_lazy:
            return f"Document(<lazy {len(self._plan_or_new().steps)} steps>)"
        backing = self._backing_obj
        where = (backing.final_url if backing else None) or (
            self.request.url if self.request else "?")
        status = backing.status_code if backing else 0
        return f"Document({where!r}, status={status}, {self._backing_name})"


register_lazy_type("Document", Document)
register_lazy_type("Element", Element)
