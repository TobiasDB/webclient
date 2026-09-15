# STABLE

The last commit whose full gate is green (`gen_stubs --check`, `mypy --strict`,
`pyright`, `pytest`, `demo.py`). Roll back here if autonomous work broke something:

    git reset --hard 4eca1869a79aeefe56be524c4306c2077885c205

- **commit:** `4eca1869a79aeefe56be524c4306c2077885c205`
- **subject:** Review + re-review fixes (correctness/ergonomics/architecture); 326 passed
- **date:** 2026-09-15

_Updated after each green milestone. 276 passed; verified against ~30 live real-site
runs (summary/readability, crawls, sitemaps, events, LLM lazy-expression tasks, and
error handling)._
