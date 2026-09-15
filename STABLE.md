# STABLE

The last commit whose full gate is green (`gen_stubs --check`, `mypy --strict`,
`pyright`, `pytest`, `demo.py`). Roll back here if autonomous work broke something:

    git reset --hard 4bef9347eca8e96427e2df035baa9836e5f9f7e4

- **commit:** `4bef9347eca8e96427e2df035baa9836e5f9f7e4`
- **subject:** Crawl: lean default summary facets + type to_blob/explain on lazy surfaces
- **date:** 2026-09-15

_Updated after each green milestone. Supersedes the Milestone-D pointer 23379c2,
which was red on test_demo_is_pyright_clean (demo edited after its gate ran); this
commit re-runs the full gate green (gen_stubs --check, mypy, pyright, 275 passed,
demo.py)._
