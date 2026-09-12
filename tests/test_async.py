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
        CARDS, content_type="text/html")
    url = httpserver.url_for("/cards")

    async def main():
        async with AsyncWebClient() as ac:
            document = await ac.execute(ac.fetch(url))      # lazy fetch, awaited
            assert document.ok and document.title == "Shop"

            rows = await ac.execute(
                ref.resolve().select_all(".card")
                .extract(t=doc.select(".title").attr("text")).project(),
                ac.ref(url))
            assert sorted(r["t"] for r in rows) == ["Aeropress", "Grinder"]

            streamed = [row async for row in ac.execute(
                ref.resolve().select_all(".title").attr("text"),
                ac.ref(url), stream=True)]
            assert sorted(streamed) == ["Aeropress", "Grinder"]

    asyncio.run(main())
