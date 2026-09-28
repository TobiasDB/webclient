"""``Proxy`` -- a shared backend concept: how a backend routes its traffic.

A proxy is more than a URL: it may carry credentials and a bypass list, and httpx and Playwright
want it in different shapes. ``Proxy`` is the one config both backends take (a bare string is
accepted and wrapped); it renders itself for each -- an auth-embedded URL for httpx, a dict for
Playwright.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from pydantic import BaseModel


class Proxy(BaseModel):
    """A proxy endpoint. ``server`` is e.g. ``http://host:8080`` or ``socks5://host:1080``;
    ``username``/``password`` are optional credentials; ``bypass`` is a comma-separated host list
    that should skip the proxy (honoured by the browser backend)."""

    server: str
    username: str | None = None
    password: str | None = None
    bypass: str | None = None

    def httpx(self) -> str:
        """A proxy URL for httpx, with credentials embedded (percent-encoded)."""
        if self.username is None:
            return self.server
        scheme, sep, rest = self.server.partition("://")
        auth = quote(self.username) + (":" + quote(self.password) if self.password else "")
        return f"{scheme}{sep}{auth}@{rest}" if sep else f"{auth}@{self.server}"

    def playwright(self) -> dict[str, str]:
        """A proxy dict for Playwright's ``launch(proxy=...)``."""
        out: dict[str, str] = {"server": self.server}
        if self.username is not None:
            out["username"] = self.username
        if self.password is not None:
            out["password"] = self.password
        if self.bypass is not None:
            out["bypass"] = self.bypass
        return out


def as_proxy(proxy: "str | Proxy | None") -> "Proxy | None":
    """Coerce a bare server string (or None, or a Proxy) into a :class:`Proxy`."""
    if proxy is None or isinstance(proxy, Proxy):
        return proxy
    return Proxy(server=proxy)


__all__ = ["Proxy", "as_proxy"]
