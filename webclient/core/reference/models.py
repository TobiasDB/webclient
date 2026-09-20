"""Reference's model + interface.

``IReference`` is the request spec: its Core Fields (the data) plus, under
``TYPE_CHECKING``, the eager ops ``Reference`` implements (generated from the
reference backings). ``Reference`` inherits it and adds only behaviour
(backings, dispatch, ``_derive``). ``HttpMethod`` (the method type its fields use)
lives here too, so the ``derive`` backing and ``from_url`` share it with no cycle.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel

HttpMethod = Literal["get", "post", "put", "patch", "delete", "head", "options"]

if TYPE_CHECKING:
    from ...clients import WaitConfig  # noqa: F401  (resolve wait strategy)
    from . import Reference  # noqa: F401  (the ops return the core itself)
    from ..document import Document  # noqa: F401  (Reference.resolve -> Document)
    from ...interface import LazyReference  # noqa: F401


class IReference(BaseModel):
    """A (re)resolvable request spec: the Core Fields, plus (for the checker) the
    eager ops ``Reference`` implements -- ``url`` / ``with_params`` / ``replace``
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
    #: an explicit content-kind hint for the response (``None`` = pure sniffing, the
    #: default -- no hint, so a wrong guess is never forced). Set it (e.g. ``"json"``)
    #: when a server mislabels or omits its ``Content-Type`` and you know the kind;
    #: it overrides the sniffer for this request.
    expect: Literal["html", "json", "xml", "binary"] | None = None

    if TYPE_CHECKING:
        # >>> generated: Reference interface <<<
        # fmt: off
        @property
        def url(self) -> str: ...
        def join(self, href: str) -> "Reference": ...
        def replace(self, **fields: Any) -> "Reference": ...
        def resolve(self, *, browser: "bool | Literal['never', 'auto', 'always']" = ..., policy: 'dict[str, Any] | None' = ..., optional: bool = ..., error: Any = ..., keep_alive: 'bool | float' = ..., wait: 'WaitConfig | None' = ...) -> "Document": ...
        def with_params(self, **params: str) -> "Reference": ...
        # fmt: on
        # >>> end generated <<<
        pass


# The Resolve policy models (retry / rate / proxy / anti-bot / browser, ``BrowserConfig``,
# the ``Resolve`` bundle, ``AUTO`` / ``resolve_policy``) now live in their own clean space,
# :mod:`webclient.policy`; the reference/client import them from there.


__all__ = ["HttpMethod", "IReference"]
