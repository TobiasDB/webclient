"""The transport identity + pool -- fetch's durable core, mirroring ``resolve/base.py``.

A :class:`Profile` bundles a transport identity (proxy / fingerprint / headers / browser realness /
impersonation) once, is inheritable via ``.with_(...)``, and knows the backend it describes
(:meth:`Profile.fetcher`) so it drops straight into a resolve escalation ladder. A
:class:`ClientPool` keeps one shared backend per profile alive (so a browser launches once), and
:func:`default_pool` is the process-wide one the functional entries lease from. The functional
``fetch()`` face itself lives in :mod:`web.fetch.entry`.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .browser import BrowserFetcher, BrowserManager
from .fingerprint import Fingerprint
from .http import HttpFetcher
from .impersonate import ImpersonateFetcher
from .proxy import Proxy


class _Keep:
    """The 'unchanged' sentinel for :meth:`Profile.with_` (so ``None`` can be passed explicitly)."""


_KEEP = _Keep()


@dataclass(frozen=True)
class Profile:
    """A reusable transport identity: proxy + fingerprint + default headers + whether to drive a
    browser. Combine once, inherit with ``.with_(...)``; a per-call ``fetch`` kwarg overrides it.
    """

    proxy: "str | Proxy | None" = None
    fingerprint: "bool | Fingerprint" = False
    headers: "dict[str, str]" = field(default_factory=dict)
    browser: bool = False
    #: when driving a browser, run it HEADED (a real windowed Chrome) instead of headless. A headed
    #: browser has far fewer automation tells than headless -- the next rung of "realness" a resolve
    #: ladder climbs to when a headless render is still blocked.
    headless: bool = True
    #: the browser CHANNEL: ``chromium`` (Playwright's bundled build) or ``chrome`` / ``chrome-beta``
    #: / ``msedge`` (the REAL, installed browser -- the most authentic identity, top of the ladder).
    channel: str = "chromium"
    #: an explicit browser binary to launch (a driver/executable path) when ``browser`` is set --
    #: for a pinned/self-managed Chromium; ``None`` uses the bundled/channel browser.
    executable_path: "str | None" = None
    #: IMPERSONATE a real browser's TLS/HTTP2 fingerprint at the HTTP layer (curl_cffi) -- a browser
    #: preset like ``"chrome"`` (empty = plain httpx). Rung 2 of the evasion ladder: it closes the
    #: JA3/JA4 + HTTP2 tell a stock client leaks (ANTI-BOT.md §2.1) without the cost of a browser.
    impersonate: str = ""
    #: drive a browser tier through the LEAK-PATCHED driver (patchright) instead of stock Playwright,
    #: suppressing the CDP ``Runtime.enable`` leak (ANTI-BOT.md §5's flagship automation tell). Only
    #: affects browser profiles; falls back to Playwright when patchright isn't installed.
    stealth: bool = False

    def with_(
        self,
        *,
        proxy: "str | Proxy | None | _Keep" = _KEEP,
        fingerprint: "bool | Fingerprint | _Keep" = _KEEP,
        headers: "dict[str, str] | _Keep" = _KEEP,
        browser: "bool | _Keep" = _KEEP,
        headless: "bool | _Keep" = _KEEP,
        channel: "str | _Keep" = _KEEP,
        executable_path: "str | None | _Keep" = _KEEP,
        impersonate: "str | _Keep" = _KEEP,
        stealth: "bool | _Keep" = _KEEP,
    ) -> "Profile":
        """A copy with some slots overridden (the rest inherited) -- adjust a base profile."""
        return Profile(
            proxy=self.proxy if isinstance(proxy, _Keep) else proxy,
            fingerprint=(self.fingerprint if isinstance(fingerprint, _Keep) else fingerprint),
            headers=self.headers if isinstance(headers, _Keep) else headers,
            browser=self.browser if isinstance(browser, _Keep) else browser,
            headless=self.headless if isinstance(headless, _Keep) else headless,
            channel=self.channel if isinstance(channel, _Keep) else channel,
            executable_path=(
                self.executable_path if isinstance(executable_path, _Keep) else executable_path
            ),
            impersonate=self.impersonate if isinstance(impersonate, _Keep) else impersonate,
            stealth=self.stealth if isinstance(stealth, _Keep) else stealth,
        )

    def fetcher(
        self, *, manager: "BrowserManager | None" = None
    ) -> "BrowserFetcher | ImpersonateFetcher | HttpFetcher":
        """The backend this transport identity describes -- so a fetch profile can be used directly
        as a tier in a resolve profile's escalation ladder (an HTTP tier, an impersonating HTTP tier,
        a browser tier, ...). A browser wins over impersonation wins over plain HTTP. ``manager`` is
        the shared browser-process manager (a pool hands in its own so every tier reuses one
        Playwright); ignored by the HTTP tiers.
        """
        if self.browser:
            return BrowserFetcher(
                headless=self.headless,
                channel=self.channel,
                proxy=self.proxy,
                fingerprint=self.fingerprint,
                executable_path=self.executable_path,
                driver="patchright" if self.stealth else "playwright",
                manager=manager,
            )
        if self.impersonate:  # rung 2: a real TLS/HTTP2 fingerprint at the HTTP layer (curl_cffi)
            return ImpersonateFetcher(impersonate=self.impersonate, proxy=self.proxy)
        return HttpFetcher(proxy=self.proxy, fingerprint=self.fingerprint)

    def key(self) -> "tuple[object, ...]":
        """A hashable identity for pooling: two profiles with the same key share one backend."""
        fp = (
            self.fingerprint
            if isinstance(self.fingerprint, bool)
            else self.fingerprint.model_dump_json()
        )
        return (
            self.browser,
            self.headless,
            self.channel,
            self.impersonate,
            self.stealth,
            str(self.proxy),
            fp,
            tuple(sorted(self.headers.items())),
            self.executable_path,
        )


_EMPTY = Profile()


class ClientPool:
    """Keeps long-lived backends alive and hands out a SHARED one per transport :class:`Profile`,
    so a browser is launched ONCE and reused across fetches (not relaunched per request), and httpx
    connections are pooled. Backends multiplex (httpx pools connections; a browser opens a context
    per session), so a lease is the shared instance, not an exclusive checkout. The pool OWNS the
    backends' lifetimes -- ``aclose`` closes them all (that is what shuts the browser).
    """

    def __init__(self) -> None:
        self._backends: (
            "dict[tuple[object, ...], BrowserFetcher | ImpersonateFetcher | HttpFetcher]"
        ) = {}
        #: ONE browser-process manager shared by every browser tier this pool leases, so the realness
        #: ladder (several browser profiles) reuses a single Playwright runtime and pools processes.
        self._browser_manager = BrowserManager()

    def lease(self, profile: "Profile") -> "BrowserFetcher | ImpersonateFetcher | HttpFetcher":
        """The shared backend for ``profile`` -- created on first use, reused thereafter."""
        key = profile.key()
        backend = self._backends.get(key)
        if backend is None:
            backend = profile.fetcher(manager=self._browser_manager)
            self._backends[key] = backend
        return backend

    async def aclose(self) -> None:
        """Close every pooled backend, then shut the shared browser manager (its Playwright + any
        launched processes still held)."""
        for backend in self._backends.values():
            await backend.aclose()
        await self._browser_manager.aclose()
        self._backends.clear()

    async def __aenter__(self) -> "ClientPool":
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()


_DEFAULT_POOL: "ClientPool | None" = None


def default_pool() -> ClientPool:
    """The process-wide default pool the functional entries lease from when none is given -- so
    repeated ``fetch(url)`` / ``resolve(url)`` calls reuse one browser / httpx client. Long-lived;
    close it with :func:`aclose_default_pool` (or own an explicit :class:`ClientPool`).
    """
    global _DEFAULT_POOL
    if _DEFAULT_POOL is None:
        _DEFAULT_POOL = ClientPool()
    return _DEFAULT_POOL


async def aclose_default_pool() -> None:
    """Close + drop the process-wide default pool (shuts any browser it launched)."""
    global _DEFAULT_POOL
    if _DEFAULT_POOL is not None:
        await _DEFAULT_POOL.aclose()
        _DEFAULT_POOL = None


__all__ = ["Profile", "ClientPool", "default_pool", "aclose_default_pool"]
