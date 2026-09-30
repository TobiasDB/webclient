"""Run the onboard eval against the lab and write ``RESULTS.md``.

    python -m eval            # from webclient/onboard, with web.onboard importable

Serves the bundled lab (pure stdlib, a daemon thread; ``eval/lab/``), runs
:func:`eval.harness.run_all`, prints the per-example Locate/Author table, and writes
``RESULTS.md`` next to this file. The lab is vendored here so the eval has no dependency on the
old monolith.
"""

from __future__ import annotations

import asyncio
from datetime import date
from pathlib import Path

from .harness import Result, run_all
from .lab import LabServer

_MARK = {"PASS": "PASS", "PARTIAL": "PART", "FAIL": "FAIL"}


def _table(results: list[Result]) -> str:
    rows = [
        "| example | category | Locate | Author | rows | detail |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for r in results:
        rows.append(
            f"| `{r.name}` | {r.category} | {r.locate_grade} | {r.author_grade} | "
            f"{r.n_rows} | {r.detail} |"
        )
    return "\n".join(rows)


def _tally(results: list[Result], attr: str) -> str:
    counts = {
        g: sum(1 for r in results if getattr(r, attr) == g) for g in ("PASS", "PARTIAL", "FAIL")
    }
    return f"PASS {counts['PASS']} · PARTIAL {counts['PARTIAL']} · FAIL {counts['FAIL']}"


def _report(results: list[Result]) -> str:
    lines = [
        "# Onboard eval -- Locate + Author against the bundled lab",
        "",
        f"_Generated {date.today().isoformat()} · Author LLM: **ClaudeShim (real model via "
        "`claude -p`, no API key)**._",
        "",
        "Two phases per example: **Locate** (find the right source -- the expected record selector, "
        "or a JSON/XML data document, preferring an XHR/data-API when one backs the page) and "
        "**Author** (write a `wq` query with a REAL model and RUN it, grading the extracted rows "
        "against the fixture's published expected result).",
        "",
        f"- **Locate:** {_tally(results, 'locate_grade')}",
        f"- **Author:** {_tally(results, 'author_grade')}",
        "",
        "> Author is driven by :class:`eval.shim.ClaudeShim` -- the local `claude -p` CLI as a raw "
        "text function (agent prompt + tools + dynamic sections stripped). No API key; it uses the "
        "Claude Code plan's usage. So this grades ACTUAL query-writing quality end to end, not a "
        "stand-in. `--model` defaults to `haiku` for cheapness.",
        "",
        _table(results),
        "",
        "## Reading the results",
        "",
        "**Locate is deterministic and strong.** It finds the dataset region (or the JSON/XML "
        "document) on every well-formed page, prefers a consistent XHR/data-API endpoint over the "
        "HTML when one backs the page, and gates blocked/auth/docs pages. It under-detects only on "
        "browser-gated pages whose records are not in the static DOM (`spa`, `feed`, `tabs`).",
        "",
        "**Author** writes the `wq` query with the real model over the patterns guide, then the "
        "pipeline compiles it safely, reroots it at the source, and runs it. A FAIL is a real "
        "query-writing miss (or a model-output parse error); a PARTIAL got rows but missed the "
        "probe. Pagination shapes author ONE page + an advisory note to follow the pager.",
    ]
    return "\n".join(lines) + "\n"


async def _main() -> None:
    srv = LabServer()
    try:
        results = await run_all(srv.base)
    finally:
        srv.close()
    for r in results:
        print(
            f"{r.name:12} {r.category:22} locate={_MARK[r.locate_grade]} "
            f"author={_MARK[r.author_grade]}  {r.detail}"
        )
    out = Path(__file__).with_name("RESULTS.md")
    out.write_text(_report(results), encoding="utf-8")
    print(f"\nwrote {out}")


if __name__ == "__main__":
    asyncio.run(_main())
