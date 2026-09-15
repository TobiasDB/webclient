# STABLE

The last commit whose full gate is green (`gen_stubs --check`, `mypy --strict`,
`pyright`, `pytest`, `demo.py`). Roll back here if autonomous work broke something:

    git reset --hard dc50be5cd6f8ece8a7e27d3805057f384ef7642c

- **commit:** `dc50be5cd6f8ece8a7e27d3805057f384ef7642c`
- **subject:** Review-driven fixes: blob=JSON, remote parity, error contract, crawl/probe/content correctness
- **date:** 2026-09-15

_Updated after each green milestone. 276 passed; verified against ~30 live real-site
runs (summary/readability, crawls, sitemaps, events, LLM lazy-expression tasks, and
error handling)._
