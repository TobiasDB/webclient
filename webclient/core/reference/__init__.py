"""ReferenceCore: the core behind a reference -- a request spec.

Core Fields = the request spec. Backings provide the derivations (``url`` prop,
``with_params`` / ``replace`` / ``join`` -- :mod:`.derive`) and ``resolve`` (via
the client -- :mod:`.resolve`). Pure data + dispatch, like every core.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar
from urllib.parse import parse_qs, urlparse

from pydantic import PrivateAttr

from ..web_core import Backing, WebCore
from .derive import DeriveBacking
from .models import HttpMethod, IReference  # noqa: F401  (HttpMethod re-exported)
from .resolve import ResolveBacking

if TYPE_CHECKING:
    from ..client import WebClientCore
    from ..session import WebSessionCore
    from ...surfaces.lazy import LazyReference


class ReferenceCore(WebCore, IReference):
    """A (re)resolvable request spec. Its Core Fields + eager ops come from the
    ``IReference`` model/interface it inherits (:mod:`.models`); this core adds the
    behaviour -- ``resolve`` dispatches to the bound client (ResolveBacking), ``url``
    and the derivations are the DeriveBacking, and ``_derive`` is the copy helper."""

    if TYPE_CHECKING:  # narrow WebCore.lazy (Any) to this core's lazy surface

        @property
        def lazy(self) -> "LazyReference": ...

    # typed non-optional: a core is bound to its client before any op runs (an
    # unbound resolve gets a default via ``WebCore._bridge_io``). ``_session`` is
    # genuinely optional (only session-scoped references have one).
    _client: "WebClientCore" = PrivateAttr(default=None)  # type: ignore[assignment]
    _session: "WebSessionCore | None" = PrivateAttr(default=None)
    _surface: Any = PrivateAttr(default=None)  # the core's single eager surface

    @property
    def ok(self) -> bool:
        return bool(self.hostname)  # a well-formed request spec

    def _derive(self, copy: "ReferenceCore") -> "ReferenceCore":
        """A derived reference: unnamed, rooted at this one, with a fresh surface
        slot (``model_copy`` carries private attrs, else the stale surface). The
        core owns this construction so the derive backing never hand-wires it."""
        copy._surface = None
        copy.name = ""
        copy.root = self.name or self.root
        return copy

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
