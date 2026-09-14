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
    from . import Reference  # noqa: F401  (the ops return the core itself)
    from ..document import Document  # noqa: F401  (Reference.resolve -> Document)
    from ...surfaces.lazy import LazyReference  # noqa: F401


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
    actions: list[dict[str, Any]] = []  # recorded live-interaction chain (reload)

    if TYPE_CHECKING:
        # >>> generated: Reference interface <<<
        # fmt: off
        @property
        def url(self) -> str: ...
        def join(self, href: str) -> "Reference": ...
        def replace(self, **fields: Any) -> "Reference": ...
        def resolve(self, *, browser: bool = ..., optional: bool = ..., error: Any = ...) -> "Document": ...
        def with_params(self, **params: str) -> "Reference": ...
        # fmt: on
        # >>> end generated <<<
        pass


# --------------------------------------------------------------------------- #
# Resolve policies -- how a reference is fetched, per concern: retry / rate /
# proxy / anti-bot / browser. Each is a frozen value model, manually configurable;
# ``AUTO`` (or the string ``"auto"``) selects a cheapest-first / escalate-on-
# evidence variant. Grouped into a ``Resolve`` bundle -- a Core Field default on
# the client/session, per-call overridable. These are just DATA: the escalation
# ladder that acts on them lives in the client transport (``afetch``). See
# docs/design/resiliency.md. (P0: the models; behaviour lands in later phases.)
# --------------------------------------------------------------------------- #


class RetryPolicy(BaseModel, frozen=True):
    """Retry a retriable failure (transport / 429 / 5xx). Generalises the client's
    ``retries`` / ``retry_backoff``."""

    max: int = 2
    backoff: Literal["exp", "const"] = "exp"
    base: float = 0.2
    on_statuses: frozenset[int] = frozenset({429, 500, 502, 503, 504})
    on_transport: bool = True
    respect_retry_after: bool = True

    @classmethod
    def auto(cls) -> "RetryPolicy":
        return cls(max=3, backoff="exp", respect_retry_after=True)


class RatePolicy(BaseModel, frozen=True):
    """Politeness: how fast to hit a host. Generalises ``min_interval`` / ``_pace``."""

    rps: float | None = None
    per: Literal["host", "global"] = "host"
    concurrency: int | None = None
    burst: int = 1
    adaptive: bool = False

    @classmethod
    def auto(cls) -> "RatePolicy":
        return cls(per="host", concurrency=1, adaptive=True)


class ProxyPolicy(BaseModel, frozen=True):
    """Route through a proxy pool; rotate a sticky exit on a block. Generalises the
    single ``proxy``."""

    pool: str | list[str] | None = None
    geo: str | None = None
    sticky: Literal["session", "host", "none"] = "session"
    rotate_on: frozenset[Any] = frozenset({403, 429, "session_error"})
    ttl: float = 600.0

    @classmethod
    def auto(cls) -> "ProxyPolicy":
        return cls(sticky="session")


class AntiBotPolicy(BaseModel, frozen=True):
    """Engage stealth / captcha handling -- ``auto`` only when a challenge is
    actually detected."""

    level: Literal["off", "stealth", "max"] = "off"
    captcha: Literal["off", "auto"] = "off"

    @classmethod
    def auto(cls) -> "AntiBotPolicy":
        return cls(level="stealth", captcha="auto")


class BrowserPolicy(BaseModel, frozen=True):
    """Render in a real browser. ``when="auto"`` renders only if the static fetch
    is empty / JS-gated / blocked (the Crawlee adaptive rule)."""

    engine: str = "chromium"
    stealth: bool = False
    wait_for: str | None = None
    when: Literal["never", "auto", "always"] = "never"

    @classmethod
    def auto(cls) -> "BrowserPolicy":
        return cls(when="auto")


class Resolve(BaseModel, frozen=True):
    """The policy bundle threaded through a fetch: one policy per concern. A Core
    Field default on the client/session; per-call overridable. ``retry``/``rate``
    always apply; ``proxy``/``antibot``/``browser`` default off (``None``)."""

    retry: RetryPolicy = RetryPolicy()
    rate: RatePolicy = RatePolicy()
    proxy: ProxyPolicy | None = None
    antibot: AntiBotPolicy | None = None
    browser: BrowserPolicy | None = None

    @classmethod
    def auto(cls) -> "Resolve":
        return cls(
            retry=RetryPolicy.auto(),
            rate=RatePolicy.auto(),
            proxy=ProxyPolicy.auto(),
            antibot=AntiBotPolicy.auto(),
            browser=BrowserPolicy.auto(),
        )


class _AutoSentinel:
    """The ``AUTO`` marker: request a concern's escalate-on-evidence variant."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "AUTO"


#: pass ``AUTO`` (or the string ``"auto"``) to a policy kwarg for its auto variant.
AUTO: Any = _AutoSentinel()


def resolve_policy(value: Any, cls: Any) -> Any:
    """Normalise a per-concern kwarg (``Policy | "auto" | AUTO | None``): a Policy
    passes through, ``AUTO``/``"auto"`` -> ``cls.auto()``, ``None`` -> ``None`` (off
    / inherit the default)."""
    if value is None:
        return None
    if value is AUTO or value == "auto":
        return cls.auto()
    return value


__all__ = [
    "HttpMethod",
    "IReference",
    "RetryPolicy",
    "RatePolicy",
    "ProxyPolicy",
    "AntiBotPolicy",
    "BrowserPolicy",
    "Resolve",
    "AUTO",
    "resolve_policy",
]
