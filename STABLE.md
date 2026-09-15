# STABLE

The last commit whose full gate is green (`gen_stubs --check`, `mypy --strict`,
`pyright`, `pytest`, `demo.py`). Roll back here if autonomous work broke something:

    git reset --hard 2e0d3f4

- **commit:** `2e0d3f4`
- **subject:** graded SPA detection + review correctness fixes + streaming leak fix (398 passed)
- **date:** 2026-09-15

_Updated after each green milestone. 398 passed; verified against live real-site
runs (example/HN/Wikipedia read not-SPA, Adobe reads SPA). demo.py + demo_summary.py
run offline._
