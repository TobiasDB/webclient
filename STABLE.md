# STABLE

The last commit whose full gate is green (`gen_stubs --check`, `mypy --strict`,
`pyright`, `pytest`, `demo.py`). Roll back here if autonomous work broke something:

    git reset --hard a13784c37bd27d44e5fb9a28254f7ab1469fad31

- **commit:** `a13784c37bd27d44e5fb9a28254f7ab1469fad31`
- **subject:** Resiliency design: confirmed decisions + re-ground on current architecture
- **date:** 2026-09-14

_Updated automatically after each green phase during the autonomous resiliency
build. If the working tree is broken, this commit is safe._
