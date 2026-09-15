# STABLE

The last commit whose full gate is green (`gen_stubs --check`, `mypy --strict`,
`pyright`, `pytest`, `demo.py`). Roll back here if autonomous work broke something:

    git reset --hard 23379c24ccc16470d82c97af584d364c506a29f0

- **commit:** `23379c24ccc16470d82c97af584d364c506a29f0`
- **subject:** Milestone D: MCP + task-verb endpoints + serializable expression blobs (#5)
- **date:** 2026-09-15

_Updated after each green milestone. Milestones A-D of the directive batch
(crawl-chosen facets + summary extra-methods + anti-bot on status; sitemap.xml
discovery; proxy/rate/retry as headers; MCP + task verbs + expr blobs) are all in
and gated. If the working tree is broken, this commit is safe._
