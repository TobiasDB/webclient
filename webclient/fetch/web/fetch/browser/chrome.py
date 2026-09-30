"""The Chrome SUPPLY -- how web.fetch OBTAINS a browser, separated from how it drives one.

A :class:`BrowserSupply` answers one question: give me a connected Playwright ``Browser`` and own
its lifecycle. Two implementations cover the realness rungs of ANTI-BOT.md §5:

  - :class:`LaunchSupply` -- launch and OWN a local process. ``channel`` picks the binary:
    ``chromium`` is Playwright's bundled build, a *Chrome for Testing* distribution whose missing
    Widevine DRM + proprietary codecs are a compiled-in tell (§5, rung 3-6); ``chrome`` /
    ``chrome-beta`` / ``msedge`` launch the GENUINE installed browser (the authentic top rung, §5
    rung 6). Headed (``headless=False``) sheds the headless tells but needs a display / Xvfb.
  - :class:`CdpSupply` -- ATTACH to an already-running Chrome over the DevTools Protocol: a real
    user profile, a grid, or a real Chrome on a residential-exit box (§5 rung 7 -- a genuine browser
    on a residential IP). Attaching to a real Chrome sidesteps the bundled-binary and launch-flag
    tells entirely; the caller must still avoid the ``Runtime.enable`` leak, which is the page
    layer's job, not this one's.

The point of the split: a caller swaps the supply -- local launch, or a remote CDP box -- without
touching :class:`~web.fetch.browser.BrowserFetcher`, which only knows how to drive pages. Binary
resolution (:func:`real_chrome_path`, :func:`ensure_chromium`) lives here too, so obtaining the
right binary is part of obtaining the browser.

Playwright is an optional extra, imported lazily by callers; this module only references its types
under ``TYPE_CHECKING`` and never imports it at module load.
"""

from __future__ import annotations

import glob
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from ..proxy import Proxy
from .display import display_needed

if TYPE_CHECKING:
    from playwright.async_api import Browser, Playwright

#: launch args that make a launched Chromium present as a real browser, not an automation harness --
#: drop the AutomationControlled blink feature (which sets navigator.webdriver + other engine-level
#: tells) and Playwright's --enable-automation switch. This is the first thing a WAF checks (§2.3).
_STEALTH_ARGS = ("--disable-blink-features=AutomationControlled",)
_DROP_DEFAULT_ARGS = ("--enable-automation",)
#: the leak-patched driver -- a drop-in Playwright fork that suppresses the CDP ``Runtime.enable``
#: leak (ANTI-BOT.md §5's flagship automation tell) and other protocol-level giveaways. Selected per
#: supply via ``driver="patchright"``; the manager starts the matching runtime, and this module
#: reuses the installed Chromium for it (its patches are in the driver, not the binary).
_LEAK_PATCHED_DRIVER = "patchright"


def _installed_chromium() -> "str | None":
    """The Playwright-managed Chromium binary, so a patched driver (patchright, which pins a
    different build) can drive the SAME binary via ``executable_path`` instead of downloading its
    own -- the leak patch lives in the driver, not the browser. Best-effort glob of the
    ms-playwright cache; ``None`` if not found (the driver then falls back to its own resolution).
    """
    base = os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or os.path.expanduser(
        "~/.cache/ms-playwright"
    )
    for pattern in (
        "chromium-*/chrome-linux*/chrome",
        "chromium-*/chrome-mac*/Chromium.app/Contents/MacOS/Chromium",
        "chromium-*/chrome-win*/chrome.exe",
    ):
        hits = sorted(glob.glob(os.path.join(base, pattern)))
        if hits:
            return hits[-1]  # the newest installed build
    return None


@runtime_checkable
class BrowserSupply(Protocol):
    """Obtains a connected Playwright ``Browser`` and owns its lifecycle. ``owns_process`` is True
    when this supply LAUNCHED the browser (so the fetcher closes it) and False when it ATTACHED to a
    running one (so cleanup only disconnects, never kills the user's / remote process)."""

    owns_process: bool
    #: which browser DRIVER runs this supply -- ``"playwright"`` (default) or ``"patchright"`` (the
    #: leak-patched fork). The manager starts the matching runtime; the supply just names it.
    driver: str

    @property
    def needs_display(self) -> bool:
        """Whether this supply's browser needs a virtual display started first (a HEADED launch on a
        displayless Linux host). The manager reads it and brings up Xvfb before connecting."""
        ...

    async def connect(self, pw: "Playwright") -> "Browser": ...

    def key(self) -> object:
        """A hashable LAUNCH identity: two supplies with the same key produce the same process, so
        the :class:`~web.fetch.browser.manager.BrowserManager` shares one between them."""
        ...


@dataclass
class LaunchSupply:
    """Launch and own a local browser process (see the module docstring for the realness rungs)."""

    headless: bool = True
    channel: str = "chromium"
    executable_path: "str | None" = None
    proxy: "Proxy | None" = None
    driver: str = "playwright"
    owns_process: bool = field(default=True, init=False)

    @property
    def needs_display(self) -> bool:
        # a headed launch on a displayless Linux host needs Xvfb; the manager provides it.
        return not self.headless and display_needed()

    def _executable(self) -> "str | None":
        if self.executable_path is not None:
            return self.executable_path
        if env := os.environ.get(
            "WEB_BROWSER_PATH"
        ):  # a pinned browser binary for every browser tier
            return env
        if self.channel != "chromium":
            # a real channel (chrome/msedge) -> resolve the GENUINE installed binary (ANTI-BOT.md §5);
            # None falls back to Playwright's own channel resolution below.
            return real_chrome_path()
        if self.driver == _LEAK_PATCHED_DRIVER:
            # patchright pins its own Chromium build; point it at the installed one (the patch is in
            # the driver, not the browser) so there is no extra download.
            return _installed_chromium()
        return None

    async def connect(self, pw: "Playwright") -> "Browser":
        executable = self._executable()
        # a resolved binary overrides the channel; keep the channel only when nothing was resolved
        # (so Playwright still tries to find e.g. a real Chrome install itself).
        channel = None if (self.channel == "chromium" or executable is not None) else self.channel
        # a HEADED browser must see DISPLAY: pass the current env EXPLICITLY so the browser doesn't
        # depend on the driver process's env, which was captured when the driver started -- possibly
        # before the manager brought up Xvfb and set DISPLAY.
        launch_env: "dict[str, str | float | bool] | None" = (
            {k: v for k, v in os.environ.items()} if not self.headless else None
        )
        return await pw.chromium.launch(
            headless=self.headless,
            channel=channel,
            executable_path=executable,
            proxy=self.proxy.playwright() if self.proxy else None,
            args=list(_STEALTH_ARGS),
            ignore_default_args=list(_DROP_DEFAULT_ARGS),
            env=launch_env,
        )

    def key(self) -> object:
        # the launch identity -- NOT the fingerprint, which is a per-CONTEXT option, so two tiers
        # that differ only by fingerprint can still share one launched process.
        return (
            "launch",
            self.driver,
            self.headless,
            self.channel,
            self.executable_path,
            str(self.proxy),
        )


@dataclass
class CdpSupply:
    """Attach to an already-running Chrome over CDP at ``endpoint`` (e.g. ``http://host:9222``). We
    attach rather than launch, so we never own or kill the process -- cleanup only disconnects."""

    endpoint: str
    driver: str = "playwright"
    owns_process: bool = field(default=False, init=False)
    #: attaching to an already-running browser -- IT owns its display, so we never start one.
    needs_display: bool = field(default=False, init=False)

    async def connect(self, pw: "Playwright") -> "Browser":
        return await pw.chromium.connect_over_cdp(self.endpoint)

    def key(self) -> object:
        return ("cdp", self.driver, self.endpoint)


#: genuine-Chrome install locations per platform (NOT the Playwright bundle / Chrome for Testing).
_CHROME_LOCATIONS: dict[str, tuple[str, ...]] = {
    "darwin": (
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Google Chrome Beta.app/Contents/MacOS/Google Chrome Beta",
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    ),
    "linux": (
        "/opt/google/chrome/chrome",
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
        "/usr/bin/microsoft-edge",
    ),
    "win32": (
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    ),
}
#: PATH names for a genuine Chrome/Edge -- deliberately NOT "chromium"/"chromium-browser", which is
#: another unbranded build with the same codec/Widevine tells as the bundle (§5).
_CHROME_EXES = ("google-chrome", "google-chrome-stable", "google-chrome-beta", "microsoft-edge")


def real_chrome_path() -> "str | None":
    """The path to a GENUINE installed Chrome/Edge (not Playwright's bundled Chrome for Testing,
    whose missing Widevine + proprietary codecs are a compiled-in tell no JS patch hides --
    ANTI-BOT.md §5). Checks PATH then the common per-OS install locations; ``None`` when no real
    Chrome is installed (then a caller falls back to the bundled Chromium tier)."""
    for name in _CHROME_EXES:
        found = shutil.which(name)
        if found is not None:
            return found
    for path in _CHROME_LOCATIONS.get(sys.platform, ()):
        if os.path.exists(path):
            return path
    return None


def ensure_chromium(*, timeout: float = 600.0) -> bool:
    """Ensure Playwright's bundled Chromium is installed (``python -m playwright install chromium``)
    -- the slim driver the headless rungs use, installed on demand so a fresh environment can render
    without a manual setup step. Returns True on success. This downloads a browser, so a caller
    invokes it deliberately (setup), never on a hot path."""
    try:
        result = subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def supply_for(
    *,
    cdp: "str | None" = None,
    headless: bool = True,
    channel: str = "chromium",
    executable_path: "str | None" = None,
    proxy: "Proxy | None" = None,
    driver: str = "playwright",
) -> BrowserSupply:
    """The default supply for a set of options: a :class:`CdpSupply` when a ``cdp`` endpoint is
    given (attach to a running / remote Chrome), else a :class:`LaunchSupply` (launch a local one).
    ``driver`` selects the browser driver (``"patchright"`` for the leak-patched fork). Lets
    :class:`~web.fetch.browser.BrowserFetcher` keep its flat kwargs while delegating the
    obtain-a-browser concern here."""
    if cdp is not None:
        return CdpSupply(endpoint=cdp, driver=driver)
    return LaunchSupply(
        headless=headless,
        channel=channel,
        executable_path=executable_path,
        proxy=proxy,
        driver=driver,
    )


__all__ = [
    "BrowserSupply",
    "LaunchSupply",
    "CdpSupply",
    "supply_for",
    "real_chrome_path",
    "ensure_chromium",
]
