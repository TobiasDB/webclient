"""Run the onboard eval against the lab and write ``RESULTS.md``.

    python -m eval            # from packages/onboard, with web.onboard + webclient importable

Serves the webclient lab (pure stdlib, a daemon thread), runs :func:`eval.harness.run_all`, prints
the per-example Locate/Author table, and writes ``RESULTS.md`` next to this file.
"""

from __future__ import annotations

import asyncio
import os
from datetime import date
from pathlib import Path

from webclient.lab import LabServer

from .harness import Result, run_all

_MARK = {"PASS": "PASS", "PARTIAL": "PART", "FAIL": "FAIL"}


def _table(results: list[Result]) -> str:
    rows = ["| example | category | Locate | Author | rows | detail |",
            "| --- | --- | --- | --- | --- | --- |"]
    for r in results:
        rows.append(f"| `{r.name}` | {r.category} | {r.locate_grade} | {r.author_grade} | "
                    f"{r.n_rows} | {r.detail} |")
    return "\n".join(rows)


def _tally(results: list[Result], attr: str) -> str:
    counts = {g: sum(1 for r in results if getattr(r, attr) == g) for g in ("PASS", "PARTIAL", "FAIL")}
    return f"PASS {counts['PASS']} · PARTIAL {counts['PARTIAL']} · FAIL {counts['FAIL']}"


def _report(results: list[Result], *, key: bool) -> str:
    engine = "AnthropicLlm (real model)" if key else "HeuristicLlm (deterministic stand-in, no API key)"
    lines = [
        "# Onboard eval -- Locate + Author against the webclient lab",
        "",
        f"_Generated {date.today().isoformat()} · Author LLM: **{engine}**._",
        "",
        "Two phases per example: **Locate** (find the right source -- the expected record selector, "
        "or a JSON/XML data document) and **Author** (write a `wq` query and RUN it, grading the "
        "rows against the fixture's published expected result).",
        "",
        f"- **Locate:** {_tally(results, 'locate_grade')}",
        f"- **Author:** {_tally(results, 'author_grade')}",
        "",
        "> With no `ANTHROPIC_API_KEY`, Author is driven by a deterministic `HeuristicLlm` that reads "
        "the prompt's skeleton + fields and writes the query by a fixed heuristic. This measures the "
        "PIPELINE MECHANICS (prompt -> parse -> reroot -> run) and how far a mechanical author gets, "
        "**not a model's selector quality** -- a real `AnthropicLlm` is a drop-in replacement.",
        "",
        _table(results),
        "",
        "## Reading the results",
        "",
        "**Locate is deterministic and strong.** It finds the dataset region (or the JSON/XML "
        "document) on every well-formed page, and prefers a consistent XHR/data-API endpoint over "
        "the HTML when one backs the page. It is imprecise only where the record region is nested "
        "(a table's `tr` vs the tighter `tbody tr`), and it under-detects on browser-gated pages "
        "whose records are not in the static DOM.",
        "",
        "**Author (with the heuristic stand-in) handles the common shapes and exposes the hard "
        "ones.** It gets the flat lists, the header-column table, the JSON/API and RSS documents, "
        "and the list-valued field. It falls short exactly where the guide tells a real model to do "
        "something structural the heuristic does not attempt:",
        "",
        "- **sibling rows / split sections / JSON islands** (`news`, `sections`, `twoface`) -- need "
        "`:scope +`, a grouped multi-section selector, or reparsing a `<script>` island; the "
        "heuristic writes a single flat `select_all`.",
        "- **transposed tables** (`pivot`) -- records are COLUMNS; the flat author does not detect "
        "that it should transpose. The guide now documents `wq.doc.tables(sel, transpose=True)` "
        "(and rowspan carry-down via `.tables(sel)`), so a real model handles both -- the heuristic "
        "cannot tell a transposed table from a normal one, so it stays PARTIAL here.",
        "- **total rows** (`ranking`) -- extracted but not filtered out.",
        "- **pagination** (`paginated`, `looppager`, `overlap`, `deep`) -- Author writes one correct "
        "page and the pipeline is told (an advisory note) to follow the pager; the single-page rows "
        "are right, so these read as PASS with a pager note.",
        "- **browser-gated** (`spa`, `feed`, `tabs`) -- the static eval resolver does not render JS "
        "or drive tabs, so the records are not in the DOM it sees; these need a browser tier.",
        "",
        "This run is against the current DSL, where `select` is **loud by default** (a miss raises "
        "`dsl.select_miss`). The heuristic marks every extract-column select `optional=True` (a "
        "mechanical author cannot know which fields are on every row), and the guide teaches "
        "`optional=True` for genuinely-optional fields and presence filters.",
        "",
        "The Author gaps are heuristic-author gaps, not pipeline gaps: the guide already documents "
        "each of these shapes, so a capable model has what it needs. The pipeline (prompt assembly, "
        "the safe compile, rerooting, running, the flag-keyed advisory notes) works on every case "
        "that produced rows.",
    ]
    return "\n".join(lines) + "\n"


async def _main() -> None:
    srv = LabServer()
    try:
        results = await run_all(srv.base)
    finally:
        srv.close()
    for r in results:
        print(f"{r.name:12} {r.category:22} locate={_MARK[r.locate_grade]} "
              f"author={_MARK[r.author_grade]}  {r.detail}")
    out = Path(__file__).with_name("RESULTS.md")
    out.write_text(_report(results, key=bool(os.environ.get("ANTHROPIC_API_KEY"))), encoding="utf-8")
    print(f"\nwrote {out}")


if __name__ == "__main__":
    asyncio.run(_main())
