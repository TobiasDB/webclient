# STABLE

The last commit whose full gate is green (`gen_stubs --check`, `mypy --strict`,
`pyright`, `pytest`, `demo.py`). Roll back here if autonomous work broke something:

    git reset --hard 345455d

- **commit:** `345455d`
- **subject:** skeleton faithful by default (no collapse; collapse=True opt-in) (389 passed)
- **date:** 2026-09-15

_Updated after each green milestone. 389 passed; verified against live real-site
runs plus a browser crawl of news.adobe.com. Both demo.py and demo_summary.py run
offline._
