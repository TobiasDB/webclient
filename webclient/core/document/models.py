"""DocumentCore's model + interface.

``IDocument`` is a resolved resource's data: its Core Fields (the response) plus,
under ``TYPE_CHECKING``, the eager ops ``DocumentCore`` implements (generated from
the document backings). ``DocumentCore`` inherits it and adds only behaviour
(backings, dispatch, ``_sub``). The ops are ``TYPE_CHECKING``-only, so at runtime
this is just the data model and ``WebCore.__getattr__`` dispatches every op.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal, TypeVar, overload  # noqa: F401

from pydantic import BaseModel

from ...errors import WebError

if TYPE_CHECKING:
    # names the generated op annotations resolve against (real cores / collections
    # / value models returned by the ops).
    from ...collection import Collection, Field  # noqa: F401
    from ...models import (  # noqa: F401
        ActionEvent,
        ConsoleEvent,
        DOMUpdateEvent,
        Element,
        Event,
        Metadata,
        Runtime,
        Structure,
        Summary,
        Transport,
    )
    from ...surfaces.lazy import LazyDocument  # noqa: F401
    from ..reference import ReferenceCore  # noqa: F401
    from . import DocumentCore  # noqa: F401

    E = TypeVar("E", bound="Event")  # events_of(type[E]) -> list[E]


class IDocument(BaseModel):
    """A resolved resource's data (the Core Fields), plus (for the checker) the
    eager ops ``DocumentCore`` implements -- ``select`` / ``attr`` / ``text_content``
    / ``render`` / the event views / the live interaction set. The ops are
    ``TYPE_CHECKING``-only, so at runtime this is just the data model."""

    # -- Core Fields (the resolved response) ---------------------------------
    id: str = ""
    name: str = ""  # scoped document name
    root: str = ""  # the originating reference's name
    session_id: str = ""  # owning session (if any)
    kind: Literal["html", "json", "xml", "binary"] = "html"
    url: str = ""
    final_url: str | None = None
    content: bytes = b""
    status_code: int = 0
    response_headers: dict[str, str] = {}
    encoding: str | None = None
    elapsed: float | None = None
    created: float = 0.0
    accessed: float = 0.0
    error: WebError | None = None

    if TYPE_CHECKING:
        # >>> generated: Document interface <<<
        # fmt: off
        @property
        def action_events(self) -> list[ActionEvent]: ...
        @property
        def console(self) -> list[ConsoleEvent]: ...
        @property
        def dom_mutations(self) -> list[DOMUpdateEvent]: ...
        @property
        def events(self) -> list[Event]: ...
        @property
        def message(self) -> str: ...
        @property
        def text_content(self) -> str: ...
        @property
        def title(self) -> str: ...
        @overload
        def attr(self, name: Literal['href', 'src', 'action']) -> "ReferenceCore": ...
        @overload
        def attr(self, name: str, *, error: Any = ...) -> "Field[str]": ...
        def click(self, selector: str | None = ..., *, timeout: float | None = ..., optional: bool = ...) -> "DocumentCore": ...
        def evaluate(self, script: str) -> "Any": ...
        @overload
        def events_of(self, event_type: type[E]) -> "list[E]": ...
        @overload
        def events_of(self, event_type: str) -> "list[Event]": ...
        def is_empty(self) -> "Field[bool]": ...
        def is_ok(self) -> "Field[bool]": ...
        def metadata(self) -> "Metadata": ...
        def ref(self) -> "ReferenceCore": ...
        def reload(self) -> "DocumentCore": ...
        @overload
        def render(self, format: Literal['elements']) -> "list[Element]": ...
        @overload
        def render(self, format: Literal['links']) -> "Collection[ReferenceCore]": ...
        @overload
        def render(self, format: str, **options: Any) -> "str": ...
        def runtime(self) -> "Runtime": ...
        def screenshot(self, selector: str | None = ...) -> "DocumentCore": ...
        def select(self, selector: str, *, index: int = ..., error: Any = ...) -> "DocumentCore": ...
        def select_all(self, selector: str, *, limit: int | None = ..., offset: int = ...) -> "Collection[DocumentCore]": ...
        def structure(self) -> "Structure": ...
        def summary(self, *include: str, exclude: Any = ...) -> "Summary": ...
        def transport(self) -> "Transport": ...
        def wait_for(self, selector: str | None = ..., *, timeout: float | None = ...) -> "DocumentCore": ...
        def write(self, selector: str, text: str, *, timeout: float | None = ..., optional: bool = ...) -> "DocumentCore": ...
        # fmt: on
        # >>> end generated <<<
        pass
