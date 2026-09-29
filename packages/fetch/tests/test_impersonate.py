"""web.fetch.impersonate -- the TLS/HTTP2-impersonating HTTP backend (curl_cffi), unit-tested with
a fake curl_cffi session so no network / compiled library is exercised."""

from __future__ import annotations

import asyncio

from web.fetch import ImpersonateFetcher, Profile, Request
from web.fetch.impersonate import _perform


def _run(coro):
    return asyncio.run(coro)


class _Resp:
    def __init__(self) -> None:
        self.status_code = 200
        self.content = b"<h1>hi</h1>"
        self.headers = {"content-type": "text/html"}
        self.url = "https://x/final"
        self.cookies = {"sess": "1"}
        self.history: list = []


class _Session:
    def __init__(self) -> None:
        self.calls: list = []

    async def request(self, method, url, **kw):
        self.calls.append((method, url, kw))
        return _Resp()


def test_impersonate_perform_builds_a_snapshot() -> None:
    s = _Session()
    snap = _run(_perform(s, Request(url="https://x/")))
    assert snap.status == 200 and snap.ok
    assert snap.content == b"<h1>hi</h1>" and snap.url == "https://x/final"
    assert snap.set_cookies == {"sess": "1"}
    method, url, _kw = s.calls[0]
    assert method == "GET" and url == "https://x/"


def test_impersonate_profile_builds_the_backend_and_keys_on_it() -> None:
    fetcher = Profile(impersonate="chrome").fetcher()
    assert isinstance(fetcher, ImpersonateFetcher)
    # impersonation is part of the pool identity, so it does not share a backend with plain httpx
    assert Profile(impersonate="chrome").key() != Profile().key()
    assert Profile(impersonate="chrome").with_(impersonate="").impersonate == ""  # inheritable


def test_impersonate_transport_error_is_classified_not_raised() -> None:
    class _Boom:
        async def request(self, *a, **k):
            raise RuntimeError("boom")

    snap = _run(_perform(_Boom(), Request(url="https://x/")))
    assert snap.error is not None and not snap.ok
