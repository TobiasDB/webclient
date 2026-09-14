"""ReferenceCore's model + interface.

``IReference`` is the request spec: its Core Fields (the data) plus, under
``TYPE_CHECKING``, the eager ops ``ReferenceCore`` implements (generated from the
reference backings). ``ReferenceCore`` inherits it and adds only behaviour
(backings, dispatch, ``_derive``). ``HttpMethod`` (the method type its fields use)
lives here too, so the ``derive`` backing and ``from_url`` share it with no cycle.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel

HttpMethod = Literal["get", "post", "put", "patch", "delete", "head", "options"]

if TYPE_CHECKING:
    from . import ReferenceCore  # noqa: F401  (the ops return the core itself)
    from ..document import DocumentCore  # noqa: F401  (Reference.resolve -> Document)
    from ...surfaces.lazy import LazyReference  # noqa: F401


class IReference(BaseModel):
    """A (re)resolvable request spec: the Core Fields, plus (for the checker) the
    eager ops ``ReferenceCore`` implements -- ``url`` / ``with_params`` / ``replace``
    / ``join`` / ``resolve``. The ops are ``TYPE_CHECKING``-only, so at runtime this
    is just the data model and ``WebCore.__getattr__`` dispatches every op."""

    # -- Core Fields (the request spec) --------------------------------------
    kind: str = "webpage"
    name: str = ""  # scoped name (the doc's `root`)
    root: str = ""  # the reference this was derived from
    hostname: str = ""
    method: HttpMethod = "get"
    scheme: str = "https"
    port: int | None = None
    path: str = ""
    fragment: str = ""
    params: dict[str, str | list[str]] = {}
    headers: dict[str, str] = {}
    cookies: dict[str, str] = {}
    body: bytes | None = None
    json_body: Any | None = None
    form: dict[str, str] | None = None
    follow_redirects: bool = True
    timeout: float | None = None
    actions: list[dict[str, Any]] = []  # recorded live-interaction chain (reload)

    if TYPE_CHECKING:
        # >>> generated: Reference interface <<<
        # fmt: off
        @property
        def url(self) -> str: ...
        def join(self, href: str) -> "ReferenceCore": ...
        def replace(self, **fields: Any) -> "ReferenceCore": ...
        def resolve(self, *, browser: bool = ..., optional: bool = ..., error: Any = ...) -> "DocumentCore": ...
        def with_params(self, **params: str) -> "ReferenceCore": ...
        # fmt: on
        # >>> end generated <<<
        pass
