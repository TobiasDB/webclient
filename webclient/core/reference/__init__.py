"""ReferenceCore: the core behind a reference -- a request spec.

Core Fields = the request spec. Backings provide the derivations (``url`` prop,
``with_params`` / ``replace`` / ``join`` -- :mod:`.derive`) and ``resolve`` (via
the client -- :mod:`.resolve`). Pure data + dispatch, like every core.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar
from urllib.parse import parse_qs, urlparse

from pydantic import BaseModel, PrivateAttr

from ..web_core import Backing, WebCore
from ._shared import DEFAULT_PORTS, HttpMethod, _derive  # noqa: F401  (re-exported)
from .derive import DeriveBacking
from .resolve import ResolveBacking

if TYPE_CHECKING:
    from ..client import WebClientCore
    from ..document import DocumentCore
    from ..session import WebSessionCore
    from ...surfaces.lazy import LazyReference

    class IReference(BaseModel):
        """The eager ops ``ReferenceCore`` implements, typed. Generated from the
        reference backings; ``ReferenceCore`` inherits it, so its ops (``url`` /
        ``join`` / ``resolve`` / ...) are statically visible on the core itself.
        Exists only for the type checker -- at runtime it is empty, so
        ``WebCore.__getattr__`` still dispatches every op."""

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

else:

    class IReference(BaseModel):  # runtime: empty -> never shadows __getattr__
        pass


class ReferenceCore(WebCore, IReference):
    """A (re)resolvable request spec. ``resolve`` (a later backing) dispatches
    to the bound client; ``url`` and the derivations are the DeriveBacking. Its
    eager ops come from the generated ``IReference`` interface it inherits."""

    if TYPE_CHECKING:  # narrow WebCore.lazy (Any) to this core's lazy surface

        @property
        def lazy(self) -> "LazyReference": ...

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

    # typed non-optional: a core is bound to its client before any op runs (an
    # unbound resolve gets a default via ``WebCore._bridge_io``). ``_session`` is
    # genuinely optional (only session-scoped references have one).
    _client: "WebClientCore" = PrivateAttr(default=None)  # type: ignore[assignment]
    _session: "WebSessionCore | None" = PrivateAttr(default=None)
    _surface: Any = PrivateAttr(default=None)  # the core's single eager surface

    @property
    def ok(self) -> bool:
        return bool(self.hostname)  # a well-formed request spec

    BACKINGS: ClassVar[tuple[Backing, ...]] = (DeriveBacking(), ResolveBacking())


def from_url(
    url: str,
    method: HttpMethod = "get",
    params: dict[str, str | list[str]] | None = None,
    headers: dict[str, str] | None = None,
    cookies: dict[str, str] | None = None,
) -> ReferenceCore:
    """Build a ReferenceCore from a URL string."""
    parsed = urlparse(url)
    query: dict[str, str | list[str]] = {
        k: v[0] if len(v) == 1 else v for k, v in parse_qs(parsed.query).items()
    }
    if params:
        query.update(params)
    return ReferenceCore(
        hostname=parsed.hostname or "",
        method=method,
        scheme=parsed.scheme or "https",
        port=parsed.port,
        path=parsed.path or "",
        fragment=parsed.fragment or "",
        params=query,
        headers=headers or {},
        cookies=cookies or {},
    )


__all__ = [
    "ReferenceCore",
    "DeriveBacking",
    "ResolveBacking",
    "HttpMethod",
    "from_url",
]
