"""Resolve policy: the clean space for how a reference is fetched.

The Resolve logic -- one frozen value model per concern (retry / rate / proxy / anti-bot /
browser), the ``BrowserConfig`` launch settings, and the ``Resolve`` bundle that threads
them through a fetch -- gathered here so the client just USES them (an ``IWebClient`` field
default, per-call overridable). These are pure DATA; the escalation ladder that acts on
them lives in the client transport (``afetch``), and :mod:`.headers` turns a ``Resolve``
into the ``X-WebClient-*`` request headers our proxy service reads. ``AUTO`` (or ``"auto"``)
selects a concern's cheapest-first / escalate-on-evidence variant.
"""

from __future__ import annotations

from typing import Any, Literal, Protocol, Self, TypeVar

from pydantic import BaseModel



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
        """The escalated retry variant: more attempts, exponential backoff, honouring Retry-After."""
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
        """The polite adaptive variant: one request at a time per host, backing off on pressure."""
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
        """The default proxy variant: a session-sticky exit (rotated on a block)."""
        return cls(sticky="session")


class AntiBotPolicy(BaseModel, frozen=True):
    """Engage stealth / captcha handling -- ``auto`` only when a challenge is
    actually detected."""

    level: Literal["off", "stealth", "max"] = "off"
    captcha: Literal["off", "auto"] = "off"

    @classmethod
    def auto(cls) -> "AntiBotPolicy":
        """The engaged variant: stealth on, captcha handled automatically."""
        return cls(level="stealth", captcha="auto")


class BrowserPolicy(BaseModel, frozen=True):
    """Render in a real browser. ``when="auto"`` renders only if the static fetch
    is empty / JS-gated / blocked (the Crawlee adaptive rule)."""

    engine: str = "chromium"
    stealth: bool = False
    wait_for: str | None = None
    #: ``never`` static-only, ``always`` straight to a browser, ``auto`` static then
    #: escalate on the response's signals (a proxy exit for a block/anti-bot
    #: challenge, a browser render for JS-gated content).
    when: Literal["never", "auto", "always"] = "never"

    @classmethod
    def auto(cls) -> "BrowserPolicy":
        """The adaptive variant: render in a browser only when the static fetch falls short."""
        return cls(when="auto")


class BrowserConfig(BaseModel, frozen=True):
    """How the client's browser is launched (a Core Field on the client). ``stealth``
    is ON by default -- the browser masks the obvious automation signals
    (``navigator.webdriver`` etc.) so a routine render isn't trivially flagged.
    ``headless`` can be turned off to drive a visible browser; ``fingerprint``
    randomises each page's user-agent / viewport / locale / timezone from a pool (a
    fresh identity per page, and the ``auto`` ladder's last anti-bot fallback)."""

    engine: str = "chromium"
    #: Playwright browser channel: ``None`` uses the bundled Chromium; ``"chrome"`` drives
    #: the machine's installed Google Chrome (the latest stable, and less bot-detectable
    #: than headless Chromium) -- set it when Chrome is installed.
    channel: str | None = None
    headless: bool = True
    stealth: bool = True
    fingerprint: bool = False
    #: how many browser PAGES the pool may hold open at once -- ONE browser process, this
    #: many concurrent pages. Raise it to run more references/sessions concurrently against
    #: a single browser (the right way to parallelise, instead of many browser processes).
    pool_pages: int = 4
    #: how many concurrent HTTP clients the pool may lease at once.
    pool_http: int = 10
    #: a proxy URL (``http://[user:pass@]host:port`` / ``socks5://…``) routing ALL of the
    #: client's traffic -- both the httpx fetches AND the browser -- through the same proxy.
    #: ``None`` = a direct connection. (A per-fetch anti-bot ``Resolve.proxy`` pool is separate;
    #: this is the always-on client-wide proxy.)
    proxy: str | None = None

    @classmethod
    def auto(cls) -> "BrowserConfig":
        """The hardened variant: stealth + a randomised fingerprint per page."""
        return cls(stealth=True, fingerprint=True)


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
        """The fully-escalated bundle -- each concern's ``auto`` variant, so a fetch adapts to
        whatever the response reveals (blocks, anti-bot, JS-gating)."""
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
        """Render as ``AUTO``."""
        return "AUTO"


#: pass ``AUTO`` (or the string ``"auto"``) to a policy kwarg for its auto variant.
AUTO: Any = _AutoSentinel()


class _HasAuto(Protocol):
    @classmethod
    def auto(cls) -> Self:
        """Build this policy's escalate-on-evidence variant."""
        ...


P = TypeVar("P", bound=_HasAuto)


def resolve_policy(value: "P | Literal['auto'] | None", cls: type[P]) -> "P | None":
    """Normalise a per-concern kwarg (``Policy | "auto" | AUTO | None``): a Policy
    passes through, ``AUTO``/``"auto"`` -> ``cls.auto()``, ``None`` -> ``None`` (off
    / inherit the default)."""
    if value is None:
        return None
    if value is AUTO or value == "auto":
        return cls.auto()
    return value

__all__ = [
    "RetryPolicy", "RatePolicy", "ProxyPolicy", "AntiBotPolicy", "BrowserPolicy",
    "BrowserConfig", "Resolve", "AUTO", "resolve_policy",
]
