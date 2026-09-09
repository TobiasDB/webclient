"""`Reference` — a request spec, and nothing else.

A Document no longer *is* a Reference. That inheritance made every Document
carry `method`, `body` and `form`, and made relative-link resolution use the
*request* URL rather than the URL that actually answered — so a link on a
page reached through a redirect resolved against the wrong base.
"""
from __future__ import annotations

from typing import Any, Literal
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

from pydantic import BaseModel, ConfigDict, PrivateAttr
from typing_extensions import Self

from .errors import WebClientError
from .ops import bind_ops, op
from .plan import Plan, Source
from .values import Expr, ExprState, register_lazy_type

HttpMethod = Literal["get", "post", "put", "patch", "delete", "head",
                     "options"]

DEFAULT_PORTS: dict[str, int] = {"http": 80, "https": 443}


class Proxy(BaseModel):
    url: str
    username: str | None = None
    password: str | None = None

    @property
    def authenticated_url(self) -> str:
        if self.username is None:
            return self.url
        scheme, _, rest = self.url.partition("://")
        auth = self.username + (f":{self.password}" if self.password else "")
        return f"{scheme}://{auth}@{rest}"


class Script(BaseModel):
    """JS injected into a browser page."""

    source: str
    run_at: Literal["init", "domcontentloaded", "load"] = "init"


@bind_ops
class Reference(Expr, BaseModel):
    """Everything needed to (re)fetch a resource."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

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

    _expr: ExprState | None = PrivateAttr(default=None)
    _client: Any = PrivateAttr(default=None)
    _session: Any = PrivateAttr(default=None)

    # -- construction --------------------------------------------------------
    @classmethod
    def from_url(cls, url: str, method: HttpMethod = "get",
                 **fields: Any) -> Self:
        parsed = urlparse(url)
        if not parsed.scheme and not parsed.netloc:
            raise WebClientError(f"not an absolute URL: {url!r}")
        query: dict[str, str | list[str]] = {
            k: v[0] if len(v) == 1 else v
            for k, v in parse_qs(parsed.query).items()
        }
        query.update(fields.pop("params", None) or {})
        return cls(hostname=parsed.hostname or "", method=method,
                   scheme=parsed.scheme or "https", port=parsed.port,
                   path=parsed.path or "", fragment=parsed.fragment or "",
                   params=query, **fields)

    @classmethod
    def coerce(cls, target: "str | Reference") -> "Reference":
        return target if isinstance(target, Reference) else cls.from_url(target)

    # -- identity ------------------------------------------------------------
    @property
    def url(self) -> str:
        port = ""
        if self.port is not None and self.port != DEFAULT_PORTS.get(self.scheme):
            port = f":{self.port}"
        url = f"{self.scheme}://{self.hostname}{port}{self.path}"
        if self.params:
            url += "?" + urlencode(self.params, doseq=True)
        if self.fragment:
            url += "#" + self.fragment
        return url

    def __repr__(self) -> str:
        if self.is_lazy:
            return f"Reference(<lazy {len(self._plan_or_new().steps)} steps>)"
        return f"Reference({self.url!r})"

    __str__ = __repr__

    # pydantic equality when eager; a recorded comparison when lazy.
    def __eq__(self, other: Any) -> Any:      # type: ignore[override]
        if self.is_lazy:
            return self._binop("eq", other)
        return BaseModel.__eq__(self, other)

    def __ne__(self, other: Any) -> Any:      # type: ignore[override]
        if self.is_lazy:
            return self._binop("ne", other)
        return not BaseModel.__eq__(self, other)

    def __hash__(self) -> int:
        if self.is_lazy:
            raise TypeError("a lazy Reference is not hashable")
        return hash(self.url)

    # -- derivation ----------------------------------------------------------
    def replace(self, **fields: Any) -> Self:
        return self._rebind(self.model_copy(update=fields))

    def with_params(self, **params: str) -> Self:
        return self._rebind(
            self.model_copy(update={"params": {**self.params, **params}}))

    def join(self, href: str) -> "Reference":
        return self._rebind(Reference.from_url(urljoin(self.url, href)))

    def _rebind(self, other: Any) -> Any:
        other._client, other._session = self._client, self._session
        return other

    def bind(self, client: Any, session: Any = None) -> Self:
        copy = self.model_copy()
        copy._client, copy._session = client, session
        return copy

    @property
    def bound(self) -> Any:
        return self._client

    # -- lazy rooting --------------------------------------------------------
    def __call__(self, url: str) -> "Reference":
        """`ref("https://…")` — a plan rooted at this URL, needing no runtime
        context."""
        if not self.is_lazy:
            raise WebClientError(
                "only the lazy `ref` root is callable; use Reference.from_url")
        return self._respawn(
            Plan(source=Source(kind="reference", url=url, root="Reference")),
            "Reference")

    # -- ops -----------------------------------------------------------------
    @op(resource="http", returns="Document", cardinality="one->one")
    def resolve(self, *, browser: bool = False, session: Any = None,
                optional: bool = False, **options: Any) -> Any:
        """Turn this reference into a Document. The one verb for it."""
        from .client import default_client
        client = self._client or default_client()
        return client.core.resolve(self, browser=browser,
                                   session=session or self._session,
                                   optional=optional, **options)


def _lazy_reference() -> Reference:
    obj = Reference.model_construct()
    object.__setattr__(obj, "_expr",
                       ExprState(plan=Plan(source=Source(kind="context",
                                                         root="Reference"))))
    return obj


register_lazy_type("Reference", Reference)
