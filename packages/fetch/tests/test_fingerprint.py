"""web.fetch.fingerprint -- browserforge-sourced identities (with a static fallback)."""

from __future__ import annotations

from web.fetch.fingerprint import CHROME, Fingerprint, fleet, generate


def test_generate_is_a_realistic_desktop_identity() -> None:
    fp = generate("chrome")
    headers = fp.http_headers()
    ua = headers.get("User-Agent", "")
    assert (
        ua.startswith("Mozilla/") and ua
    )  # a real UA (browserforge, or the CHROME fallback)
    assert fp.viewport[0] >= 800  # desktop, not a phone viewport


def test_fleet_rotates_across_coherent_identities() -> None:
    fl = fleet()
    assert len(fl) >= 1
    assert all(
        f.http_headers().get("User-Agent") for f in fl
    )  # every member has a coherent UA


def test_raw_headers_are_used_verbatim() -> None:
    fp = Fingerprint(
        user_agent="x", raw_headers={"User-Agent": "generated/1.0", "Accept": "*/*"}
    )
    assert fp.http_headers() == {"User-Agent": "generated/1.0", "Accept": "*/*"}
    assert CHROME.http_headers()["User-Agent"].startswith(
        "Mozilla/"
    )  # the static fallback derives
