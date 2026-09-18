"""A tiny offline site the demos run against -- so every demo is self-contained and
deterministic (no network, no live sites). Import ``serve()`` for a base URL, and the
small ``h1``/``kv``/``table`` helpers for clean output.

Pages:
  /            a shop listing (static HTML -- cards with title / price / link)
  /items/<n>   a JSON product detail
  /spa         a JS-gated page (empty shell; a script injects the content)
  /feed        a page whose records are loaded by XHR from /api/items
  /api/items   the JSON the feed fetches
  /login       a sign-in wall
  /app         a live interactive page (a button adds rows to a cart)
"""

from __future__ import annotations

import http.server
import threading
from typing import Any

SHOP = b"""
<html><head><title>Roasters Coffee</title></head><body>
<nav><a href="/about">about</a><a href="/login">sign in</a></nav>
<main>
  <h1>Featured</h1>
  <div class="card"><h2 class="title">Aeropress</h2>
    <a class="link" href="/items/1">view</a>
    <span class="price" data-price="39.00">$39</span></div>
  <div class="card"><h2 class="title">Grinder</h2>
    <a class="link" href="/items/2">view</a>
    <span class="price" data-price="129.00">$129</span></div>
  <div class="card"><h2 class="title">Gooseneck Kettle</h2>
    <a class="link" href="/items/3">view</a>
    <span class="price" data-price="59.00">$59</span></div>
</main>
<footer>(c) Roasters</footer>
</body></html>
"""
ITEM = b'{"id": %d, "name": "%s", "stock": {"count": %d}, "sku": "SKU-%d"}'
SPA = b"""
<html><head><title>SPA</title></head><body>
  <div id="app"></div>
  <script>
    document.getElementById("app").innerHTML =
      '<ul class="news">' +
      [1,2,3].map(function(i){ return '<li class="item"><h3>Story ' + i +
        '</h3><time datetime="2026-09-0' + i + '">Sep ' + i + '</time></li>'; }).join('') +
      '</ul>';
  </script>
</body></html>
"""
FEED = b"""
<html><head><title>Newsroom</title></head><body>
  <main><ul id="list"></ul></main>
  <script>
    fetch('/api/items').then(function(r){ return r.json(); }).then(function(items){
      document.getElementById('list').innerHTML = items.map(function(it){
        return '<li class="item"><h3>' + it.title + '</h3>' +
               '<time datetime="' + it.date + '">' + it.date + '</time></li>';
      }).join('');
    });
  </script>
</body></html>
"""
API = b'[{"title":"Q3 earnings released","date":"2026-09-14"},' \
      b'{"title":"New roastery opens","date":"2026-09-15"},' \
      b'{"title":"Partnership announced","date":"2026-09-16"}]'
LOGIN = b"""
<html><head><title>Sign in</title></head><body>
  <form action="/session" method="post">
    <input name="email" type="email" placeholder="you@example.com">
    <input name="password" type="password">
    <button type="submit">Sign in</button>
  </form>
</body></html>
"""
APP = b"""
<html><head><title>Cart</title></head><body>
  <h1>Cart</h1>
  <input id="qty" type="text">
  <button id="add" onclick="document.querySelector('#cart').insertAdjacentHTML(
    'beforeend', '<li>item x' + document.querySelector('#qty').value + '</li>')">add</button>
  <ul id="cart"></ul>
</body></html>
"""


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?")[0]
        if path in ("/", "/about"):
            body, ctype = SHOP, "text/html; charset=utf-8"
        elif path.startswith("/items/"):
            n = int(path.rsplit("/", 1)[1])
            names = {1: b"Aeropress", 2: b"Grinder", 3: b"Gooseneck Kettle"}
            body = ITEM % (n, names.get(n, b"Item"), 7 * n, n)
            ctype = "application/json"
        elif path == "/spa":
            body, ctype = SPA, "text/html"
        elif path == "/feed":
            body, ctype = FEED, "text/html"
        elif path == "/api/items":
            body, ctype = API, "application/json"
        elif path == "/login":
            body, ctype = LOGIN, "text/html"
        elif path == "/app":
            body, ctype = APP, "text/html"
        else:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a: Any) -> None:  # quiet
        pass


def serve() -> str:
    """Start the offline site on a random localhost port; return its base URL."""
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{server.server_port}"


# -- pretty output ---------------------------------------------------------- #
def h1(title: str) -> None:
    print(f"\n\033[1m{title}\033[0m\n" + "─" * len(title))


def kv(label: str, value: Any) -> None:
    print(f"  {label:<14} {value}")


def table(rows: list[dict[str, Any]]) -> None:
    if not rows:
        print("  (no rows)")
        return
    cols = list(rows[0].keys())
    widths = {c: max(len(c), *(len(str(r.get(c, ""))) for r in rows)) for c in cols}
    line = "  " + "  ".join(c.upper().ljust(widths[c]) for c in cols)
    print(line)
    print("  " + "  ".join("─" * widths[c] for c in cols))
    for r in rows:
        print("  " + "  ".join(str(r.get(c, "")).ljust(widths[c]) for c in cols))
