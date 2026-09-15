# STABLE

The last commit whose full gate is green (`gen_stubs --check`, `mypy --strict`,
`pyright`, `pytest`, `demo.py`). Roll back here if autonomous work broke something:

    git reset --hard d00f9be

- **commit:** `d00f9be`
- **subject:** review cleanup: stale-content fix, dedup helpers, canon.py split (399 passed)
- **date:** 2026-09-15

_Updated after each green milestone. 399 passed; verified against live real-site runs
(graded SPA detection, crawl resilience). demo.py + demo_summary.py run offline._
