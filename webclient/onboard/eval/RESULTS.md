# Onboard eval -- Locate + Author against the webclient lab

_Generated 2026-09-29 · Author LLM: **ClaudeShim (real model via `claude -p`, no API key)**._

Two phases per example: **Locate** (find the right source -- the expected record selector, or a JSON/XML data document, preferring an XHR/data-API when one backs the page) and **Author** (write a `wq` query with a REAL model and RUN it, grading the extracted rows against the fixture's published expected result).

- **Locate:** PASS 21 · PARTIAL 0 · FAIL 3
- **Author:** PASS 17 · PARTIAL 0 · FAIL 7

> Author is driven by :class:`eval.shim.ClaudeShim` -- the local `claude -p` CLI as a raw text function (agent prompt + tools + dynamic sections stripped). No API key; it uses the Claude Code plan's usage. So this grades ACTUAL query-writing quality end to end, not a stand-in. `--model` defaults to `haiku` for cheapness.

| example | category | Locate | Author | rows | detail |
| --- | --- | --- | --- | --- | --- |
| `shop` | static list | PASS | PASS | 3 | 3 rows, probe title~'Aeropress' found |
| `store` | static list | PASS | PASS | 5 | 5 rows, probe name~'Ethiopia Yirgacheffe' found |
| `board` | static list | PASS | PASS | 5 | 5 rows, probe title~'Senior Engineer' found |
| `frozen` | static list | PASS | PASS | 3 | 3 rows, probe name~'Row A' found |
| `large` | static list | PASS | PASS | 6000 | 6000 rows, probe v~'value 0' found |
| `catalog` | static list | PASS | PASS | 6 | 6 rows, probe title~'A Light in the Attic' found |
| `quotes` | list + list-field | PASS | PASS | 6 | 6 rows, probe author~'Albert Einstein' found |
| `table` | table | PASS | PASS | 3 | 3 rows, probe item~'Aeropress' found |
| `ranking` | table | PASS | PASS | 3 | 3 rows, probe population~'1412' found |
| `merged` | table (rowspan) | PASS | PASS | 5 | 5 rows, probe category~'Fruit' found |
| `pivot` | table (transposed) | PASS | FAIL | 0 | no rows (query did not produce a list) |
| `api` | json | PASS | PASS | 3 | 3 rows, probe name~'Aeropress' found |
| `cursor` | json | PASS | PASS | 4 | 4 rows, probe name~'Item 1' found |
| `rss` | xml feed | PASS | FAIL | 0 | WebException: dsl.select_miss: selector 'pubdate' matched nothing |
| `paginated` | pagination | PASS | PASS | 4 | 4 rows, probe name~'Row 1' found |
| `looppager` | pagination | PASS | PASS | 4 | 4 rows, probe name~'Item 1' found |
| `overlap` | pagination | PASS | PASS | 5 | 5 rows, probe name~'Item 1' found |
| `deep` | pagination + detail | PASS | PASS | 4 | 4 rows, probe name~'Item 1' found |
| `news` | sibling rows | PASS | FAIL | 0 | WebException: dsl.select_miss: selector 'tr.athing span.titleline > a' matched nothing |
| `sections` | split sections | PASS | PASS | 4 | 4 rows, probe title~'Autumn Cupping' found |
| `twoface` | json island | PASS | FAIL | 3 | 3 rows; probe name~'Item 1' not found |
| `tabs` | tabbed | FAIL | FAIL | 0 | locate found no source |
| `spa` | browser: spa | FAIL | FAIL | 0 | locate found no source |
| `feed` | browser: xhr | FAIL | FAIL | 0 | locate found no source |

## Reading the results

**Locate is deterministic and strong.** It finds the dataset region (or the JSON/XML document) on every well-formed page, prefers a consistent XHR/data-API endpoint over the HTML when one backs the page, and gates blocked/auth/docs pages. It under-detects only on browser-gated pages whose records are not in the static DOM (`spa`, `feed`, `tabs`).

**Author** writes the `wq` query with the real model over the patterns guide, then the pipeline compiles it safely, reroots it at the source, and runs it. A FAIL is a real query-writing miss (or a model-output parse error); a PARTIAL got rows but missed the probe. Pagination shapes author ONE page + an advisory note to follow the pager.
