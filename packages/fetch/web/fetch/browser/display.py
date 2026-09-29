"""Virtual-display (Xvfb) management -- part of the browser layer, so a HEADED browser can run on a
headless Linux server.

A headed browser sheds the headless-mode tells (ANTI-BOT.md §5 -- real ``window.outerWidth``, no
``HeadlessChrome``, none of the ``--headless`` rendering gaps) and is the least-detectable Chromium
config on public scanners. But it needs an X display, and a server has none. So on a displayless
Linux host we start an **Xvfb** virtual framebuffer and point the browser at it -- exactly the same
architecture as the browser processes: the :class:`~web.fetch.browser.manager.BrowserManager` owns
one, starts it lazily for the first headed launch, and stops it on close.

Caveat worth knowing: a virtual framebuffer has no GPU, so WebGL/canvas fall back to software
rendering (SwiftShader) -- a server tell we spoof at the fingerprint layer, but not perfectly (a
canvas-vs-WebGL consistency check can still notice). The hardest targets want a real GPU / real
machine, which is the raw-CDP-to-real-Chrome path, not this.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
import time


def display_needed() -> bool:
    """Whether a HEADED browser needs a virtual display started here: Linux, no ``DISPLAY`` already
    set (a desktop / WSLg session is used as-is), and Xvfb installed. macOS/Windows always have a
    display, so this is False there."""
    if sys.platform != "linux":
        return False
    if os.environ.get("DISPLAY"):
        return False
    return shutil.which("Xvfb") is not None


class VirtualDisplay:
    """An Xvfb virtual framebuffer process and its ``DISPLAY``. :meth:`start` is a no-op (returns
    ``None``) when a real display already exists or Xvfb is unavailable; otherwise it launches Xvfb
    and returns its display string. :meth:`stop` terminates it. One instance per manager."""

    def __init__(self, *, width: int = 1920, height: int = 1080, depth: int = 24) -> None:
        self._proc: "subprocess.Popen[bytes] | None" = None
        self._size = (width, height, depth)
        #: the started display (e.g. ``":99"``), or None while not running / not needed.
        self.display: "str | None" = None

    def start(self) -> "str | None":
        """Start Xvfb (once) and return its ``DISPLAY``; None when a real display exists or Xvfb is
        missing. Blocking (spawns a process, waits for its socket) -- the manager offloads it."""
        if self.display is not None:
            return self.display
        if not display_needed():
            return None
        num = _free_display_number()
        width, height, depth = self._size
        proc = subprocess.Popen(
            ["Xvfb", f":{num}", "-screen", "0", f"{width}x{height}x{depth}", "-nolisten", "tcp"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        # readiness = the lock file Xvfb writes once it owns the display. (The `/tmp/.X11-unix` socket
        # is not a reliable signal everywhere -- WSL's Xvfb runs without creating it.)
        lock = f"/tmp/.X{num}-lock"
        for _ in range(100):  # up to ~5s
            if proc.poll() is not None:  # Xvfb died on startup
                return None
            if os.path.exists(lock):
                self._proc = proc
                self.display = f":{num}"
                return self.display
            time.sleep(0.05)
        proc.terminate()  # never came up -> don't leak the process
        return None

    def stop(self) -> None:
        if self._proc is not None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
            self._proc = None
        self.display = None


def _free_display_number(start: int = 99, end: int = 200) -> int:
    # a display number is free only when NEITHER its socket NOR its lock file exists -- a stale
    # `/tmp/.X<n>-lock` (from a crashed Xvfb) makes a new server fail "already active".
    for num in range(start, end):
        if os.path.exists(f"/tmp/.X11-unix/X{num}") or os.path.exists(f"/tmp/.X{num}-lock"):
            continue
        return num
    return start


async def astart(display: VirtualDisplay) -> "str | None":
    """Start ``display`` off the event loop (its start blocks on a process + socket wait)."""
    return await asyncio.to_thread(display.start)


__all__ = ["VirtualDisplay", "display_needed", "astart"]
