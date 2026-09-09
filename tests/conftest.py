import pytest

from webclient import WebClient


@pytest.fixture
def wc():
    with WebClient() as client:
        yield client


CARDS = """<html><head><title>Laptops</title></head><body>
  <h1>Laptops</h1>
  <div class="card"><h3>X1 Carbon</h3><span class="price">1299</span>
    <a href="/i/1">go</a></div>
  <div class="card"><h3>T14</h3><span class="price">980</span>
    <a href="/i/2">go</a></div>
  <div class="card"><h3>Mystery</h3><span class="price"></span>
    <a href="/missing">go</a></div>
</body></html>"""


@pytest.fixture
def site(httpserver):
    httpserver.expect_request("/cards").respond_with_data(
        CARDS, content_type="text/html")
    for n in (1, 2):
        httpserver.expect_request(f"/i/{n}").respond_with_json(
            {"id": n, "salary": f"{n}0k", "company": f"co-{n}"})
    httpserver.expect_request("/missing").respond_with_data(
        "nope", status=404, content_type="text/html")
    httpserver.expect_request("/redir").respond_with_data(
        "", status=302, headers={"Location": "/deep/page"})
    httpserver.expect_request("/deep/page").respond_with_data(
        '<html><body><a href="c.html">rel</a></body></html>',
        content_type="text/html")
    return httpserver
