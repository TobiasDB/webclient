#!/usr/bin/env python
"""A tour of everything webclient currently does, against a local server.

Run: python demo.py            (add --no-browser to skip the playwright part)

Every section is a feature that exists; nothing here is aspirational.
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from webclient import (DROP_ROW, NULL, RAISE_ERROR, Document, Plan,
                       RequestRecord, ResolveError, StaleDocument,
                       UnsupportedOperation, WebClient, doc, el, err, field,
                       ref)

# --------------------------------------------------------------------------- #
# A tiny site to scrape
# --------------------------------------------------------------------------- #

LISTING = """<html><head><title>Coffee Shop</title></head><body>
<h1>Beans</h1>
<p class="result-count">3 results</p>
<div class="card"><h3>Ethiopia Guji</h3><span class="price">18.00</span>
  <a href="/item/1">details</a></div>
<div class="card"><h3>Kenya Nyeri</h3><span class="price">21.50</span>
  <a href="/item/2">details</a></div>
<div class="card"><h3>Mystery Lot</h3><span class="price"></span>
  <a href="/item/404">details</a></div>
<a class="next" href="/page/2">next page</a>
</body></html>"""

PAGE2 = """<html><body><h1>Beans</h1>
<div class="card"><h3>Colombia Huila</h3><span class="price">17.00</span>
  <a href="/item/1">details</a></div>
</body></html>"""

CATEGORIES = """<html><body>
<div class="cat"><h2>Filter</h2>
  <div class="item"><span class="label">Guji</span><span class="sku">F1</span></div>
  <div class="item"><span class="label">Nyeri</span><span class="sku">F2</span></div>
</div>
<div class="cat"><h2>Espresso</h2>
  <div class="item"><span class="label">Huila</span><span class="sku">E1</span></div>
</div></body></html>"""

LIVE = """<html><head><title>Live</title></head><body>
<h1>Dashboard</h1>
<table id="rows">
  <tr class="row"><td class="name">alpha</td><td class="size">1</td></tr>
  <tr class="row"><td class="name">beta</td><td class="size">2</td></tr>
</table>
<button id="more">load more</button>
<script>
document.getElementById('more').addEventListener('click', () => setTimeout(() => {
  const tr = document.createElement('tr');
  tr.className = 'row';
  tr.innerHTML = '<td class="name">gamma</td><td class="size">3</td>';
  document.getElementById('rows').appendChild(tr);
}, 150));
</script></body></html>"""

SECOND = "<html><head><title>Second</title></head><body><h1>Second</h1></body></html>"


class Handler(BaseHTTPRequestHandler):
    hits: dict[str, int] = {}

    def log_message(self, *args: object) -> None:
        pass

    def do_GET(self) -> None:                                # noqa: N802
        path = self.path
        Handler.hits[path] = Handler.hits.get(path, 0) + 1
        if path == "/listing":
            return self._send(LISTING)
        if path == "/page/2":
            return self._send(PAGE2)
        if path == "/categories":
            return self._send(CATEGORIES)
        if path == "/live":
            return self._send(LIVE)
        if path == "/second":
            return self._send(SECOND)
        if path.startswith("/item/") and path != "/item/404":
            n = path.rsplit("/", 1)[-1]
            return self._send(json.dumps(
                {"id": n, "origin": f"origin-{n}", "roast": "medium"}),
                content_type="application/json")
        if path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/deep/final")
            self.end_headers()
            return
        if path == "/deep/final":
            return self._send('<html><body><a href="sibling.html">rel</a>'
                              "</body></html>")
        if path == "/flaky":
            status = 503 if Handler.hits[path] == 1 else 200
            return self._send("<p>steady</p>", status=status)
        if path == "/set-cookie":
            self.send_response(200)
            self.send_header("Set-Cookie", "token=s3cret; Path=/")
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<p>ok</p>")
            return
        if path == "/whoami":
            cookie = self.headers.get("Cookie", "(none)")
            return self._send(f"<p>{cookie}</p>")
        self._send("<h1>Not Found</h1>", status=404)

    def _send(self, body: str, *, status: int = 200,
              content_type: str = "text/html") -> None:
        payload = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def serve() -> tuple[HTTPServer, str]:
    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_port}"


# --------------------------------------------------------------------------- #

def title(text: str) -> None:
    print(f"\n\033[1m{text}\033[0m\n" + "─" * len(text))


def show(label: str, value: object) -> None:
    print(f"  {label:<22} {value}")


# --------------------------------------------------------------------------- #

def core(wc: WebClient, base: str) -> Document:
    title("1. Resolving — one verb, one Document")
    listing = wc.resolve(f"{base}/listing")
    show("status", listing.status_code.get())
    show("final_url", listing.final_url.get())
    show("title", listing.attr("title").get())
    show("h1", listing.select("h1").attr("text").get())
    show("cards", len(listing.select_all(".card")))
    show("backing", listing._backing_name)

    title("2. One accessor — attr(), real and pseudo attributes")
    card = listing.select(".card", index=1)
    show('attr("text")', card.select("h3").attr("text").get())
    show('attr("href")', card.select("a").attr("href").url)
    show("element address", card.path)
    show('attr("nope")', card.attr("nope", optional=True).get())

    title("3. Links resolve against the URL that answered")
    redirected = wc.resolve(f"{base}/redirect")
    show("requested", f"{base}/redirect")
    show("answered", redirected.final_url.get())
    show("relative link", redirected.select("a").attr("href").url)
    show("redirect hops", [r.status for r in redirected.telemetry.redirects])

    title("4. Representations — one render op, format by name")
    show("markdown", listing.render("markdown").get().splitlines()[0])
    show("elements", [b.type for b in listing.render("elements").get()][:5])
    show("links", len(listing.render("links").get()))
    show("formats", wc.renderers.formats("html"))

    def shouty(document: Document) -> str:
        return document.select("h1").attr("text").get().upper()

    wc.renderers.register("html", "shouty", shouty)
    show('custom "shouty"', listing.render("shouty").get())

    title("5. Capability is runtime state, and errors say so")
    show("supports(browser)", listing.supports("browser"))
    try:
        listing.click(".next")
    except UnsupportedOperation as exc:
        show("click()", str(exc))

    title("6. Telemetry — records, no bus")
    seen: list[RequestRecord] = []
    wc.on(RequestRecord, seen.append)
    wc.resolve(f"{base}/listing")
    show("observed", [(r.status, f"{r.ms:.0f}ms") for r in seen])
    show("per-document", listing.telemetry)
    return listing


def expressions(listing: Document, base: str, wc: WebClient) -> None:
    title("7. then / map — evaluated now, on a real Document")
    record = listing.then(
        doc.select("h1").attr("text").alias("category"),
        count=doc.select(".result-count").attr("text"),
        rows=doc.select_all(".card").map(
            el.select("h3").attr("text").alias("title"),
            price=el.select(".price").attr("text").otherwise(NULL),
            link=el.select("a").attr("href"),
        ),
    )
    show("category", record["category"])
    show("count", record["count"])
    for row in record["rows"]:
        show("  row", {"title": row["title"], "price": row["price"],
                       "link": row["link"].url})

    title("8. Extractions are stored on the Document")
    show("doc.fields", sorted(listing.fields))
    show('field("category")', listing.field("category").get())

    title("9. filter, explode, limit")
    priced = (listing.select_all(".card")
              .map(el.select("h3").attr("text").alias("title"),
                   price=el.select(".price").attr("text"))
              .filter(field("price") != ""))
    show("filtered", [r["title"] for r in priced])

    categories = Document.from_content(CATEGORIES, url=f"{base}/categories")
    nested = categories.select_all(".cat").map(
        el.select("h2").attr("text").alias("name"),
        items=el.select_all(".item").map(
            el.select(".label").attr("text").alias("label"),
            sku=el.select(".sku").attr("text")),
    )
    show("nested", [(r["name"], len(r["items"])) for r in nested])
    show("exploded", [dict(r) for r in nested.explode("items")])

    title("10. The same expression, recorded instead of run")
    plan = doc.select_all(".card").map(
        el.select("h3").attr("text").alias("title"))
    show("eager", [r["title"] for r in listing.select_all(".card").map(
        el.select("h3").attr("text").alias("title"))])
    show("lazy", [r["title"] for r in plan.collect(listing)])
    print()
    print("  " + plan.explain().replace("\n", "\n  "))

    title("11. Plans are the serialisation")
    blob = plan.to_plan().model_dump_json()
    show("json bytes", len(blob))
    from webclient.execute import collect_plan
    restored = Plan.model_validate_json(blob)
    show("re-run", [r["title"] for r in collect_plan(restored, listing)])

    title("12. otherwise — recovery keeps what the failure told you")
    followed = (
        ref(f"{base}/listing").resolve()
        .then(
            doc.select("h1").attr("text").alias("board"),
            rows=doc.select_all(".card").map(
                el.select("h3").attr("text").alias("title"),
                link=el.select("a").attr("href"),
                detail=field("link").resolve().then(
                    origin=doc.query("origin"),
                    roast=doc.query("roast"),
                ).otherwise(
                    status=doc.status_code,
                    message=err.message,
                    where=err.op,
                ),
            ),
        )
        .otherwise(RAISE_ERROR)
    ).collect(client=wc)
    show("board", followed["board"])
    for row in followed["rows"]:
        detail = row["detail"]
        summary = (f"ok origin={detail['origin']}" if detail["ok"]
                   else f"FAILED status={detail['status']} {detail['message']}")
        show(f"  {row['title'][:16]}", summary)


def sessions_and_pages(wc: WebClient, base: str) -> None:
    title("13. Sessions carry cookies and headers")
    session = wc.session(headers={"X-Demo": "1"})
    session.resolve(f"{base}/set-cookie")
    show("session cookies", session.cookies)
    show("echoed back", session.resolve(f"{base}/whoami")
         .select("p").attr("text").get())
    show("session", session)

    title("14. Pagination")
    first = wc.resolve(f"{base}/listing")
    pages = list(wc.paginate(first, ".next"))
    show("pages walked", len(pages))
    show("titles", [p.select("h1").attr("text").get() for p in pages])

    title("15. Middleware — retries are an ordered chain")
    from webclient.middleware import observe, redirects, retry
    with WebClient(middleware=[redirects(), retry(2, on_status=(503,)),
                               observe()]) as flaky_client:
        recovered = flaky_client.resolve(f"{base}/flaky")
        show("final status", recovered.status_code.get())
        show("retries", [(r.attempt, r.reason)
                         for r in recovered.telemetry.retries])

    title("16. scrape() — composition, not machinery")
    out = wc.scrape(f"{base}/listing", formats=("markdown", "links"))
    show("status", out["status"])
    show("markdown head", out["formats"]["markdown"].splitlines()[0])
    show("links", len(out["formats"]["links"]))

    title("17. Errors are typed")
    try:
        wc.resolve(f"{base}/item/404")
    except ResolveError as exc:
        show("ResolveError", f"{exc} (document attached: "
                             f"{exc.document is not None})")
    lenient = wc.resolve(f"{base}/item/404", optional=True)
    show("optional=True", f"ok={lenient.ok.get()} "
                          f"message={lenient.message.get()}")


def browser(wc: WebClient, base: str) -> None:
    title("18. The browser backing — same ops, different backing")
    try:
        live = wc.resolve(f"{base}/live", browser=True)
    except Exception as exc:                                  # pragma: no cover
        show("skipped", f"{type(exc).__name__}: {exc}")
        return
    try:
        show("backing", live._backing_name)
        show("supports(browser)", live.supports("browser"))
        show("rows before", len(live.select_all(".row")))

        element = live.select(".row", index=1)
        show("element address", element.path)

        live.click("#more").wait_stable(quiet_ms=250)
        show("rows after", len(live.select_all(".row")))
        show("address still valid",
             element.select(".name").attr("text").get())

        title("19. The same plan over both backings")
        plan = doc.select_all(".row").map(
            el.select(".name").attr("text").alias("name"),
            size=el.select(".size").attr("text"))
        static = wc.resolve(f"{base}/live")
        show("http backing", [dict(r) for r in plan.collect(static)])
        show("page backing", [dict(r) for r in plan.collect(live)][:2])

        title("20. navigate() — the old Document keeps its snapshot")
        second = live.navigate("/second")
        show("new document", second.select("h1").attr("text").get())
        show("old, static half", live.render("markdown").get().splitlines()[0])
        show("old, supports()", live.supports("browser"))
        try:
            live.click("#more")
        except StaleDocument as exc:
            show("old, live half", str(exc))

        shot = second.screenshot()
        show("screenshot", f"{shot.kind.get()}, "
                           f"{len(shot.content.get())} bytes")
        show("console", second.telemetry.console)
        wc.release(second)
        show("pages held", wc.stats().pages_held)
    finally:
        try:
            wc.release(live)
        except Exception:
            pass


def async_passthrough(wc: WebClient, base: str) -> None:
    title("21. Async is a pass-through, not a second API")
    import anyio

    async def main() -> None:
        document = await wc.core.resolve(f"{base}/listing")
        rendered = await document.core.render("markdown")
        show("await wc.core.resolve", document.status_code.get())
        show("await doc.core.render", rendered.get().splitlines()[0])

    anyio.run(main)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    server, base = serve()
    print(f"\033[2mserving {base}\033[0m")
    try:
        with WebClient() as wc:
            listing = core(wc, base)
            expressions(listing, base, wc)
            sessions_and_pages(wc, base)
            if not args.no_browser:
                browser(wc, base)
            async_passthrough(wc, base)
        print("\n\033[1mdone.\033[0m")
    finally:
        server.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
