# STABLE

The last commit whose full gate is green (`gen_stubs --check`, `mypy --strict`,
`pyright`, `pytest`, `demo.py`). Roll back here if autonomous work broke something:

    git reset --hard 434024b

- **commit:** `434024b`
- **subject:** load-time DOM mutations + content_from_xhr; browser/auto crawl defaults; skeleton HTML; SPA fix; packaged lazy-query skill (382 passed)
- **date:** 2026-09-15

_Updated after each green milestone. 382 passed; verified against ~30 live real-site
runs plus a live browser crawl of news.adobe.com (scored/sorted frontier surfaces
nav/"read more" links first; runtime correctly reads is_spa=True, framework=aem-edge,
content_from_xhr=True). Both demo.py and demo_summary.py run offline._
