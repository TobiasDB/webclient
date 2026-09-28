"""``Fingerprint`` -- a shared backend concept: the identity a backend presents.

A fingerprint is a coherent bundle -- a user-agent, the client-hint headers that must match it, a
viewport, and a locale -- rendered per backend (HTTP headers for httpx; context options + a
stealth patch for the browser). Keeping them together avoids the classic mismatch (a Chrome UA
with no ``Sec-Ch-Ua``, or a desktop UA in a mobile viewport). It is NOT TLS/JA3 impersonation --
that is a heavier transport swap (curl_cffi) that plugs into the HTTP backend; this is the
header/context layer both backends share. ``fingerprint=True`` on a backend uses :data:`CHROME`.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class Fingerprint(BaseModel):
    """A presented identity. Extra ``headers`` are merged after the derived client hints."""

    user_agent: str
    accept_language: str = "en-US,en;q=0.9"
    viewport: tuple[int, int] = (1280, 800)
    locale: str = "en-US"
    platform: str = "Windows"
    ua_brands: str = '"Chromium";v="124", "Not-A.Brand";v="99"'
    headers: dict[str, str] = {}

    def http_headers(self) -> dict[str, str]:
        """The request headers this identity sends (UA + matching client hints + extras)."""
        return {
            "User-Agent": self.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": self.accept_language,
            "Sec-Ch-Ua": self.ua_brands,
            "Sec-Ch-Ua-Platform": f'"{self.platform}"',
            "Upgrade-Insecure-Requests": "1",
            **self.headers,
        }

    def context_options(self) -> dict[str, Any]:
        """Options for Playwright's ``new_context`` (UA, viewport, locale) matching this identity."""
        w, h = self.viewport
        return {"user_agent": self.user_agent, "viewport": {"width": w, "height": h}, "locale": self.locale}


#: a Chrome-on-Windows desktop fingerprint (the ``fingerprint=True`` default).
CHROME = Fingerprint(
    user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
)


def as_fingerprint(fp: "bool | Fingerprint | None") -> "Fingerprint | None":
    """Coerce ``True`` -> :data:`CHROME`, ``False``/``None`` -> None, a Fingerprint -> itself."""
    if fp is True:
        return CHROME
    if not fp:
        return None
    return fp


__all__ = ["Fingerprint", "CHROME", "as_fingerprint"]
