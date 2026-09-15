# STABLE

The last commit whose full gate is green (`gen_stubs --check`, `mypy --strict`,
`pyright`, `pytest`, `demo.py`). Roll back here if autonomous work broke something:

    git reset --hard 5efb395

- **commit:** `5efb395`
- **subject:** DOM-stability default for browser loads + scored/sorted crawl frontier (371 passed)
- **date:** 2026-09-15

_Updated after each green milestone. 371 passed; verified against ~30 live real-site
runs (summary/readability, crawls, sitemaps, events, LLM lazy-expression tasks, and
error handling), plus a live browser crawl of news.adobe.com confirming the scored/
sorted frontier surfaces nav / "read more" links first._
