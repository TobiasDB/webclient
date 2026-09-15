# STABLE

The last commit whose full gate is green (`gen_stubs --check`, `mypy --strict`,
`pyright`, `pytest`, `demo.py`). Roll back here if autonomous work broke something:

    git reset --hard 3ea68a2

- **commit:** `3ea68a2`
- **subject:** review cycle — keyword/boilerplate scoring fixes + region op (lxml out of crawl) + wait bound (373 passed)
- **date:** 2026-09-15

_Updated after each green milestone. 373 passed; verified against ~30 live real-site
runs (summary/readability, crawls, sitemaps, events, LLM lazy-expression tasks, and
error handling), plus a live browser crawl of news.adobe.com confirming the scored/
sorted frontier surfaces nav / "read more" links first._
