"""ReferenceCore: the core behind a reference -- a request spec (rewrite
skeleton).

Core Fields = the request spec; backings provide ``resolve`` (via the client),
the pure derivations (``with_params``/``replace``/``join``) and ``url`` /
``from_url``. Pure data + dispatch, like every core.
"""
from __future__ import annotations

from typing import Any, ClassVar, Literal

from pydantic import BaseModel, PrivateAttr

from .web_core import Backing, WebCore

HttpMethod = Literal["get", "post", "put", "patch", "delete", "head", "options"]


class ReferenceCore(WebCore, BaseModel):
    """A (re)resolvable request spec. ``resolve`` dispatches to a backing that
    calls the bound client; ``url`` is a derived read; ``from_url`` constructs
    one. None of it lives as methods on the generated surface."""

    # -- Core Fields (the request spec) --------------------------------------
    kind: str = "webpage"
    hostname: str = ""
    method: HttpMethod = "get"
    scheme: str = "https"
    port: int | None = None
    path: str = ""
    fragment: str = ""
    params: dict[str, str | list[str]] = {}
    headers: dict[str, str] = {}
    cookies: dict[str, str] = {}
    # TODO(port): body/json_body/form/follow_redirects/timeout/actions/options.

    _client: Any = PrivateAttr(default=None)
    _session: Any = PrivateAttr(default=None)

    # -- backings: from_url / url (derivation) + resolve (client) ------------
    BACKINGS: ClassVar[tuple[Backing, ...]] = ()   # TODO: (RefDerive, Resolve)

    # TODO(port): url_of / from_url / request_fields / bind -- the clean
    #   Reference helpers, re-homed as a derivation backing + constructors.


__all__ = ["ReferenceCore", "HttpMethod"]
