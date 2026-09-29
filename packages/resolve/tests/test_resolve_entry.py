"""web.resolve functional entry -- resolve() one-shot / session / pagination kwarg."""

from __future__ import annotations

import asyncio
from typing import Any

from pytest_httpserver import HTTPServer
from web.fetch import Profile as FetchProfile
from web.resolve import EscalationPolicy, PaginatePolicy, Profile, RetryPolicy, resolve


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def test_resolve_one_shot_returns_a_document(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/").respond_with_data(
        b"<title>Home</title>", content_type="text/html"
    )

    async def go() -> str:
        doc = await resolve(httpserver.url_for("/"))  # awaited -> one-shot Document
        return doc.metadata().title or ""

    assert _run(go()) == "Home"


def test_resolve_as_session_reuses_state(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/a").respond_with_data(b"<title>A</title>", content_type="text/html")
    httpserver.expect_request("/b").respond_with_data(b"<title>B</title>", content_type="text/html")

    async def go() -> tuple[str, str]:
        async with resolve(httpserver.url_for("/a")) as session:  # same call, as a session
            a = await session.doc()
            b = await session.resolve(httpserver.url_for("/b"))
            return a.metadata().title or "", b.metadata().title or ""

    assert _run(go()) == ("A", "B")


def test_resolve_session_interacts_with_the_live_page(httpserver: HTTPServer) -> None:
    from web.resolve import profiles

    # a page whose DOM only changes after a click -- proves resolve -> interact -> re-resolve
    page = (
        b"<html><body><div id='box'>before</div>"
        b"<button id='go' onclick=\"document.getElementById('box').textContent='after'\">go"
        b"</button></body></html>"
    )
    httpserver.expect_request("/app").respond_with_data(page, content_type="text/html")

    async def go() -> tuple[str, str]:
        async with resolve(httpserver.url_for("/app"), profile=profiles.FULL_BROWSER) as s:
            before = (await s.doc()).select_all("#box")[0].text  # the live page, parsed
            await s.click("#go")  # interaction on the live page
            after = (await s.doc()).select_all("#box")[0].text  # re-parsed -> reflects the click
            return before, after

    assert _run(go()) == ("before", "after")


def test_resolve_pagination_is_a_plain_kwarg(httpserver: HTTPServer) -> None:
    for n in (1, 2):
        httpserver.expect_request("/feed", query_string=f"page={n}").respond_with_data(
            f'<main><article class="row">p{n}</article></main>'.encode(),
            content_type="text/html",
        )

    async def go() -> list[str]:
        # pagination is just a kwarg -- no Resolver, no middleware assembled by hand
        doc = await resolve(
            httpserver.url_for("/feed") + "?page=1",
            paginate=PaginatePolicy(param="page", max_pages=2),
        )
        return [e.text for e in doc.select_all("article.row")]

    assert _run(go()) == ["p1", "p2"]


def test_resolve_profile_uses_fetch_profiles_in_its_ladder(
    httpserver: HTTPServer,
) -> None:
    httpserver.expect_request("/").respond_with_data(
        b"<title>Vendor</title>", content_type="text/html"
    )
    # the resolve profile owns policy (retry/escalation); its ladder is FETCH profiles (transport
    # identity per tier) -- http base, escalate to a browser tier on a block signal
    vendor = Profile(
        escalation=EscalationPolicy(tiers=(FetchProfile(), FetchProfile(browser=True))),
        retry=RetryPolicy(max_attempts=2),
    )

    async def go() -> str:
        doc = await resolve(
            httpserver.url_for("/"), profile=vendor
        )  # http tier serves; browser unused
        return doc.metadata().title or ""

    assert _run(go()) == "Vendor"


def test_default_policy_profiles_resolve(httpserver: HTTPServer) -> None:
    from web.resolve import profiles

    httpserver.expect_request("/").respond_with_data(
        b"<title>Profiled</title>", content_type="text/html"
    )

    async def go() -> str:
        # the BASIC policy: rotating identity + retry + politeness, applied by name
        doc = await resolve(httpserver.url_for("/"), profile=profiles.BASIC)
        return doc.metadata().title or ""

    assert _run(go()) == "Profiled"
