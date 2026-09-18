"""08 · Provenance: which request produced this data?

THE POINT: on a JS page, the records you see were injected by some XHR/fetch. webclient
captures the request timeline and, in the skeleton, attributes each DOM region to the
request that produced it (``← after req[n]``) -- so you (or the model) can see the page is
backed by ``/api/items`` and just query the JSON API directly, or trust that the scraped
region is real data and not chrome. Attribution is inference (arbitrary JS sits in between),
so it is confidence-scored and degrades safely -- never a false certainty.

(Needs chromium.)
"""

from webclient import WebClient
from _site import serve, h1, kv


def main() -> None:
    base = serve()
    with WebClient() as wc:
        feed = wc.fetch(f"{base}/feed", browser="always")

        h1("The request timeline + the record region, attributed")
        for line in feed.skeleton().splitlines():
            # show the correlation header + the marked record lines
            if any(k in line for k in ("XHR/fetch", "api/items", "RECORD LIST", "after req", "<li")):
                print("  " + line)

        h1("The data API behind the page")
        kv("data API", sorted({c.url for c in feed.xhr_endpoints()}))
        kv("records", [r.attr("text") for r in feed.select_all("li.item h3")])

    h1("Why it matters")
    kv("skip the DOM", "see it's /api/items -> query the JSON directly (cleaner, faster)")
    kv("trust", "the scraped region is attributed to a real response, not guessed")
    kv("evidence", "confidence-scored; WEBCLIENT_CORRELATOR=content adds value-matching")


if __name__ == "__main__":
    main()
