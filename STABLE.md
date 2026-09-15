# STABLE

The last commit whose full gate is green (`gen_stubs --check`, `mypy --strict`,
`pyright`, `pytest`, `demo.py`). Roll back here if autonomous work broke something:

    git reset --hard 835c14ee6ace77c9c6d1ccbba972cf1e514a5f8e

- **commit:** `835c14ee6ace77c9c6d1ccbba972cf1e514a5f8e`
- **subject:** skeleton exposed (tools/MCP/API/summary) + robust multi-provider search (368 passed)
- **date:** 2026-09-15

_Updated after each green milestone. 276 passed; verified against ~30 live real-site
runs (summary/readability, crawls, sitemaps, events, LLM lazy-expression tasks, and
error handling)._
