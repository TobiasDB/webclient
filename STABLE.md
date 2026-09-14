# STABLE

The last commit whose full gate is green (`gen_stubs --check`, `mypy --strict`,
`pyright`, `pytest`, `demo.py`). Roll back here if autonomous work broke something:

    git reset --hard 89a2f9ebf8b935ef0aac73ccb8a942e3f8dde354

- **commit:** `89a2f9ebf8b935ef0aac73ccb8a942e3f8dde354`
- **subject:** Resiliency design: confirmed decisions + re-ground on current architecture
- **date:** 2026-09-14

_Updated automatically after each green phase during the autonomous resiliency
build. If the working tree is broken, this commit is safe._
