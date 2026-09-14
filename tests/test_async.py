"""AsyncWebClient: the same eager surface, awaited at the IO boundary. The async
client is just a different dispatcher on the core -- ``await ac.fetch(url)``
resolves on the engine loop and bridges to the caller's loop; in-memory ops on
the resolved document are synchronous; deeper IO chains go through ``ac.lazy``
plans (``await ...acollect()`` / ``.astream()``)."""

import asyncio

from webclient import AsyncWebClient, doc, ref

CARDS = """
<html><head><title>Shop</title></head><body>
  <div class="card"><span class="title">Aeropress</span></div>
  <div class="card"><span class="title">Grinder</span></div>
</body></html>
"""


def test_async_fetch_execute_and_stream(httpserver):
    httpserver.expect_request("/cards").respond_with_data(
        CARDS, content_type="text/html"
    )
    url = httpserver.url_for("/cards")

    async def main():
        async with AsyncWebClient() as ac:
            # await ac.fetch(url): the eager async boundary -> a Document
            document = await ac.fetch(url)
            assert document.ok and document.title == "Shop"
            # in-memory ops on the resolved document are synchronous
            titles = document.select_all(".title")
            assert [t.text_content for t in titles] == ["Aeropress", "Grinder"]

            # IO ops on the async surface are awaitable, so a chain stays async:
            # ref -> resolve, and doc -> select -> resolve
            page = await ac.ref(url).resolve()
            assert page.select(".title").text_content == "Aeropress"

            # a deeper IO chain: a lazy plan, realised with acollect() (ac.ref(url)
            # is an eager Reference context)
            rows = await (
                ref.resolve()
                .select_all(".card")
                .extract(t=doc.select(".title").text_content)
                .project()
                .acollect(ac.ref(url))
            )
            assert sorted(r["t"] for r in rows) == ["Aeropress", "Grinder"]

            streamed = [
                row
                async for row in ref.resolve()
                .select_all(".title")
                .text_content.astream(ac.ref(url))
            ]
            assert sorted(streamed) == ["Aeropress", "Grinder"]

    asyncio.run(main())
