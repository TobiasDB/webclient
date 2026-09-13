"""ReferenceCore: the core behind a reference -- a request spec.

Core Fields = the request spec. Backings provide the derivations (``url`` prop,
``with_params`` / ``replace`` / ``join``) and ``resolve`` (via the client).
Pure data + dispatch, like every core.
"""
from __future__ import annotations

from typing import Any, ClassVar, Literal
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

from pydantic import BaseModel, PrivateAttr

from .web_core import Backing, WebCore

HttpMethod = Literal["get", "post", "put", "patch", "delete", "head", "options"]
DEFAULT_PORTS: dict[str, int] = {"http": 80, "https": 443}


class DeriveBacking(Backing):
    """Pure request-spec derivations (no IO). ``url`` is a property op; the
    rest return a fresh ``ReferenceCore``."""

    props = frozenset({"url"})
    provides = frozenset({"with_params", "replace", "join"})
    gate = "ok"

    def url(self, core: "ReferenceCore") -> str:
        port = ""
        if core.port is not None and core.port != DEFAULT_PORTS.get(core.scheme):
            port = f":{core.port}"
        out = f"{core.scheme}://{core.hostname}{port}{core.path}"
        if core.params:
            out += "?" + urlencode(core.params, doseq=True)
        if core.fragment:
            out += "#" + core.fragment
        return out

    def replace(self, core: "ReferenceCore", **fields: Any) -> "ReferenceCore":
        return core.model_copy(update=fields)

    def with_params(self, core: "ReferenceCore", **params: str) -> "ReferenceCore":
        return core.model_copy(update={"params": {**core.params, **params}})

    def join(self, core: "ReferenceCore", href: str) -> "ReferenceCore":
        return from_url(urljoin(self.url(core), href))


class ReferenceCore(WebCore, BaseModel):
    """A (re)resolvable request spec. ``resolve`` (a later backing) dispatches
    to the bound client; ``url`` and the derivations are the DeriveBacking."""

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

    BACKINGS: ClassVar[tuple[Backing, ...]] = (DeriveBacking(),)
    # TODO: + ResolveBacking (resolve via the client).


def from_url(url: str, method: HttpMethod = "get",
             params: dict[str, str | list[str]] | None = None,
             headers: dict[str, str] | None = None,
             cookies: dict[str, str] | None = None) -> ReferenceCore:
    """Build a ReferenceCore from a URL string."""
    parsed = urlparse(url)
    query: dict[str, str | list[str]] = {
        k: v[0] if len(v) == 1 else v for k, v in parse_qs(parsed.query).items()}
    if params:
        query.update(params)
    return ReferenceCore(
        hostname=parsed.hostname or "", method=method,
        scheme=parsed.scheme or "https", port=parsed.port,
        path=parsed.path or "", fragment=parsed.fragment or "",
        params=query, headers=headers or {}, cookies=cookies or {})


__all__ = ["ReferenceCore", "DeriveBacking", "HttpMethod", "from_url"]
