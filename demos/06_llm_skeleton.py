"""06 · Why the LLM writes a CORRECT selector: the skeleton.

THE POINT (vs "just hand Claude the HTML"): raw HTML is huge, noisy, and provenance-free --
the model guesses a selector and often gets 0 rows. webclient hands the model a token-lean
skeleton that MARKS the dataset (``← RECORD LIST · select_all(...)``), shows where a field's
value actually lives (``data-price="39.00"`` when the text is ``$39``), flags non-obvious
controls, and (on a browser render) tags client-injected nodes ``[xhr]`` and attributes each
region to the request/click that produced it. The model reads intent, not tag soup.
"""

from webclient import WebClient
from _site import serve, h1, kv


def main() -> None:
    base = serve()
    with WebClient() as wc:
        page = wc.fetch(f"{base}/")

        h1("Raw HTML the model would otherwise wade through")
        raw = page.render("html")
        kv("size", f"{len(raw)} chars of tag soup (and real pages are 100x this)")

        h1("The skeleton the model actually reads")
        for line in page.skeleton().splitlines():
            print("  " + line)

    h1("What the model can now see at a glance")
    kv("the dataset", '"← RECORD LIST · 3 items · select_all(\\"div.card\\")"')
    kv("the value", 'price text is "$39" but the number lives in data-price="39.00"')
    kv("provenance", "on a browser render: [xhr]/[js] + '← after req[n]' per region")

    h1("Why it matters")
    kv("correct 1st try", "the model selects the marked container, not a lookalike -> rows, not 0")
    kv("right attribute", "it reads data-price, not the formatted text -> clean values")
    kv("cheaper", "a lean skeleton is a fraction of the tokens of raw HTML")


if __name__ == "__main__":
    main()
