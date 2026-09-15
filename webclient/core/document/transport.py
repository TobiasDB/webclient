"""TransportBacking: the ``transport`` facet -- transport facts of a resolved
response (values only for the few that are the projection; headers/cookies as key
lists). A pure projection: it reads what the resolution captured, never fetches."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..web_core import Backing
from .models import Transport

if TYPE_CHECKING:
    from . import Document


def _cdn(h: dict[str, str]) -> str | None:
    """A best-effort CDN name from response headers (lowercased keys/values)."""
    via, server = h.get("via", ""), h.get("server", "")
    if "cf-ray" in h or "cloudflare" in server:
        return "cloudflare"
    if "x-amz-cf-id" in h or "cloudfront" in via:
        return "cloudfront"
    if "x-served-by" in h or "fastly" in server or "fastly" in via:
        return "fastly"
    if "x-akamai-transformed" in h or "akamai" in server:
        return "akamai"
    if "x-vercel-id" in h:
        return "vercel"
    return None


class TransportBacking(Backing):
    """The ``transport`` facet: transport facts from the resolved response
    (values only for the few that are the summary; headers/cookies as keys)."""

    provides = frozenset({"transport"})
    gate = "ok"

    def applies(self, core: "Document") -> bool:
        return True

    def transport(self, core: "Document") -> Transport:
        h = {k.lower(): v for k, v in core.response_headers.items()}
        return Transport(
            final_url=core.final_url or core.url,
            status_code=core.status_code,
            ok=core.ok,
            kind=core.kind,
            redirect_chain=(
                [core.url] if core.final_url and core.final_url != core.url else []
            ),
            duration_ms=round(core.elapsed * 1000, 1) if core.elapsed else None,
            content_type=h.get("content-type"),
            encoding=core.encoding,
            size_bytes=len(core.content) or None,
            header_keys=sorted(core.response_headers),
            set_cookie_keys=sorted(core._set_cookies),
            server=h.get("server"),
            cdn=_cdn(h),
            region=h.get("cf-ipcountry") or h.get("x-country"),
            escalation=list(core._tiers) or ["static"],
            final_tier=core._tiers[-1] if core._tiers else "static",
        )


__all__ = ["TransportBacking"]
