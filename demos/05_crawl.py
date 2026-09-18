"""05 · A crawler is a first-class object, not a for-loop you write.

THE POINT (vs Playwright): to crawl with Playwright you hand-roll the frontier, the dedup,
the scope rules, the scoring, the concurrency. webclient's ``crawl`` is a scored, scoped
traversal you drive one turn at a time or let self-drive -- it retains a lean descriptor per
page (rebuild a Reference from it) and the unresolved, best-first-scored frontier.
"""

from webclient import WebClient
from _site import serve, h1, kv


def main() -> None:
    base = serve()
    with WebClient() as wc:
        h1("Drive it one turn at a time, or let it self-drive")
        with wc.crawl(f"{base}/", max_pages=5, browser=False) as crawl:
            crawl.step()  # one turn: fetch the seed, discover + score its links
            kv("frontier", [(round(e.score, 2), e.url.replace(base, "") or "/")
                            for e in crawl.frontier])
            crawl.run()   # drain the rest (dedup + scope enforced by the client)
            kv("visited", [(p.final_url or p.url).replace(base, "") or "/"
                           for p in crawl.pages])
            kv("page card", crawl.pages[0].model_dump(
                include={"url", "kind", "status_code", "title"}))

    h1("Why it matters")
    kv("scored", "links ranked best-first (nav / 'read more' high, footer low)")
    kv("scoped", "same-origin + dedup handled for you; no infinite loops")
    kv("lean", "keeps a PageCard per page (rebuild a Reference), not full DOMs")
    kv("Playwright", "all of the above is code you write and maintain yourself")


if __name__ == "__main__":
    main()
