"""AsyncWebClient: the same facade helpers, awaited. Same plans as the sync
client, executed on the engine loop and bridged to the caller's loop."""

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
            # acollect(): the async twin of collect() -- the async realization
            document = await ac.fetch(url).acollect()
            assert document.ok and document.title == "Shop"
            titles = await ac.fetch(url).select_all(".title").acollect()
            assert [t.text for t in titles] == ["Aeropress", "Grinder"]

            rows = await (
                ref.resolve()
                .select_all(".card")
                .extract(t=doc.select(".title").attr("text"))
                .project()
                .acollect(ac.ref(url))
            )
            assert sorted(r["t"] for r in rows) == ["Aeropress", "Grinder"]

            streamed = [
                row
                async for row in ref.resolve()
                .select_all(".title")
                .attr("text")
                .astream(ac.ref(url))
            ]
            assert sorted(streamed) == ["Aeropress", "Grinder"]

    asyncio.run(main())
