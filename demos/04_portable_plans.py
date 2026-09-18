"""04 · The query is a portable artifact you can store, ship and inspect.

THE POINT: a webclient query is a serialisable Plan. Encode it to a short blob, put it in a
database or a config file, hand it to another process or a remote service, and rebuild + run
it later -- WITHOUT the code that authored it. You can also render it: a SQL-EXPLAIN tree, or
a self-contained HTML wireframe of the whole extraction. A Playwright script is code; this is
data you can treat like data.
"""

from webclient import WebClient, from_blob, wq
from _site import serve, h1, kv


def main() -> None:
    base = serve()

    query = (
        wq.reference(base)
        .resolve()
        .select_all(".card")
        .extract(
            title=wq.doc.select(".title").attr("text"),
            price=wq.doc.select(".price").attr("data-price"),
        )
        .project()
    )

    h1("Encode to a portable blob")
    blob = query.to_blob()
    kv("blob", blob[:88] + " …")
    kv("bytes", len(blob))

    h1("Rebuild it in a fresh process -- no authoring code needed")
    rebuilt = from_blob(blob)
    kv("describe", rebuilt.describe())
    with WebClient() as wc:
        rows = rebuilt.collect(wc.ref(base))
    kv("runs", f"{len(rows)} rows, e.g. {rows[0]}")

    h1("Inspect it: a SQL-EXPLAIN tree")
    for line in query.explain().splitlines():
        print("  " + line)

    h1("Or picture it: a self-contained HTML wireframe")
    html = query.wireframe()
    kv("wireframe", f"{len(html)} bytes of inline-CSS/SVG HTML (open in a browser)")
    # write it next to the demo so a stakeholder can open it
    from pathlib import Path

    out = Path(__file__).with_name("query_wireframe.html")
    out.write_text(html, encoding="utf-8")
    kv("saved", out.name)

    h1("Why it matters")
    kv("versionable", "store the blob in git / a DB; diff it; roll it back")
    kv("portable", "ship it to a worker or a remote service; it runs the same")
    kv("inspectable", "explain()/wireframe() -- review a query without running it")


if __name__ == "__main__":
    main()
