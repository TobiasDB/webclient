"""Case study: a news-article scraper.

Site: https://text.npr.org/ -- NPR's text-only edition. It is a deliberately
lightweight, accessibility-focused mirror (tiny pages, stable markup, minimal JS),
which makes it a friendly, low-impact target for a demo scraper.

What it does: read the front page, pick the top story links, and for each story
pull the headline, the readable body, and a token-lean summary -- the fields a
reading-list tool or an LLM summariser actually wants.

Features: fetch, render("text"/"markdown"), summary(), render("links"),
Reference.resolve(). Run:  env/bin/python examples/news_article_scraper.py
"""

from __future__ import annotations

from webclient import WebClient, WebException

FRONT = "https://text.npr.org/"
UA = "webclient-examples/0.1 (+https://github.com/TobiasDB/webclient)"
N_ARTICLES = 3


def _clean(text: str, n: int = 280) -> str:
    text = " ".join(text.split())
    return text[:n] + ("…" if len(text) > n else "")


def article_links(front) -> list:
    """Story links on the text-edition front page have paths like ``/nx-s1-<id>``."""
    seen, out = set(), []
    for ref in front.render("links"):
        if "/nx-s1-" in ref.path and ref.path not in seen:
            seen.add(ref.path)
            out.append(ref)
    return out


def scrape() -> None:
    with WebClient(default_headers={"User-Agent": UA}, min_interval=0.4, timeout=25) as wc:
        front = wc.fetch(FRONT)
        print(f"front page: {front.title!r}  ({front.status_code})")

        links = article_links(front)[:N_ARTICLES]
        print(f"found {len(links)} story links; scraping {len(links)}\n")

        for i, ref in enumerate(links, 1):
            try:
                page = ref.resolve()
            except WebException as exc:  # one bad article shouldn't sink the run
                print(f"[{i}] {ref.path}: {exc.error.type} (skipped)")
                continue

            s = page.summary()  # token-lean structured overview
            body = page.render("text", main_content_only=True)
            print(f"[{i}] {page.title}")
            print(f"     url:     {page.final_url or page.url}")
            if s.structure:
                print(f"     length:  {s.structure.word_count} words "
                      f"(~{s.structure.reading_time_min} min read)")
            print(f"     lead:    {_clean(body)}")
            print()


if __name__ == "__main__":
    try:
        scrape()
    except WebException as exc:  # the site itself is down / blocking
        print(f"scrape failed: {exc.error.type} -- {exc} (retriable={exc.error.retriable})")
