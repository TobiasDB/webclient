"""09 · A stateful, multi-step flow as ONE declarative plan.

THE POINT (vs Playwright): a login-then-scrape or a click-through-tabs flow in Playwright is
imperative code holding a live page. webclient expresses it as a SINGLE plan: ``.step(action)``
chains interactions against one held page, the interleaved ``.extract(...)`` captures accumulate,
and ``.project()`` renders them. The executor resolves the page once, HOLDS it across every step
(and any sub-resolve), and releases it when done. One plan, one page, a clean result.

(Needs chromium.)
"""

from webclient import WebClient, wq
from _site import serve, h1, kv


def main() -> None:
    base = serve()

    flow = (
        wq.ref.resolve(browser="always")
        .step(wq.doc.write("#qty", "7"))          # type a quantity
        .step(wq.doc.click("#add"))               # add to cart
        .step(wq.doc.wait_for("#cart li"))        # wait for the row
        .extract(first=wq.doc.select("#cart li").attr("text"))
        .step(wq.doc.write("#qty", "9"))          # do it again
        .step(wq.doc.click("#add"))
        .extract(second=wq.doc.select("#cart li", index=1).attr("text"))
        .project()
    )

    h1("The whole flow, as one plan")
    print("  " + flow.describe())

    with WebClient() as wc:
        result = wc.execute(flow, wc.ref(f"{base}/app"))

    h1("Result (both captures, from one held page)")
    kv("captured", result)

    h1("Why it matters")
    kv("declarative", "a stateful flow is data, not a script babysitting a page")
    kv("lifecycle", "the page is held across steps + sub-resolves, released when done")
    kv("serialisable", "the sequence is one plan -- describe()/blob like any other query")


if __name__ == "__main__":
    main()
