# STABLE

The last commit whose full gate is green (`gen_stubs --check`, `mypy --strict`,
`pyright`, `pytest`, `demo.py`). Roll back here if autonomous work broke something:

    git reset --hard e2452e4

- **commit:** `e2452e4`
- **subject:** fix invalid crawl URLs from whitespace hrefs + cap returned frontier to width (385 passed)
- **date:** 2026-09-15

_Updated after each green milestone. 385 passed; verified against live real-site
runs plus a browser crawl of news.adobe.com. Both demo.py and demo_summary.py run
offline._
