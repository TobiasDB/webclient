# STABLE

The last commit whose full gate is green (`gen_stubs --check`, `mypy --strict`,
`pyright`, `pytest`, `demo.py`). Roll back here if autonomous work broke something:

    git reset --hard 1d79e049cc23ab1188dd6d0fc364cea852067062

- **commit:** `1d79e049cc23ab1188dd6d0fc364cea852067062`
- **subject:** Live dogfood fix: browser render emits a NavigationEvent (static/browser parity)
- **date:** 2026-09-15

_Updated after each green milestone. 276 passed; verified against ~30 live real-site
runs (summary/readability, crawls, sitemaps, events, LLM lazy-expression tasks, and
error handling)._
