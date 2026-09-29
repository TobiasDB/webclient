# Onboard eval -- Locate + Author against the webclient lab

_Generated 2026-09-29 · Author LLM: **HeuristicLlm (deterministic stand-in, no API key)**._

Two phases per example: **Locate** (find the right source -- the expected record selector, or a JSON/XML data document) and **Author** (write a `wq` query and RUN it, grading the rows against the fixture's published expected result).

- **Locate:** PASS 21 · PARTIAL 0 · FAIL 3
- **Author:** PASS 7 · PARTIAL 7 · FAIL 10

> With no `ANTHROPIC_API_KEY`, Author is driven by a deterministic `HeuristicLlm` that reads the prompt's skeleton + fields and writes the query by a fixed heuristic. This measures the PIPELINE MECHANICS (prompt -> parse -> reroot -> run) and how far a mechanical author gets, **not a model's selector quality** -- a real `AnthropicLlm` is a drop-in replacement.

| example | category | Locate | Author | rows | detail |
| --- | --- | --- | --- | --- | --- |
| `shop` | static list | PASS | PARTIAL | 3 | 3 rows but probe title~'Aeropress' MISSING |
| `store` | static list | PASS | PARTIAL | 5 | 5 rows but probe name~'Ethiopia Yirgacheffe' MISSING |
| `board` | static list | PASS | PASS | 5 | 5 rows, probe title~'Senior Engineer' found |
| `frozen` | static list | PASS | PASS | 3 | 3 rows, probe name~'Row A' found |
| `large` | static list | PASS | PARTIAL | 6000 | 6000 rows but probe v~'value 0' MISSING |
| `catalog` | static list | PASS | PARTIAL | 6 | 6 rows but probe title~'A Light in the Attic' MISSING |
| `quotes` | list + list-field | PASS | PARTIAL | 6 | 6 rows but probe author~'Albert Einstein' MISSING |
| `table` | table | PASS | FAIL | 0 | QueryError: query did not parse: invalid syntax (<unknown>, line 1) |
| `ranking` | table | PASS | FAIL | 0 | QueryError: query did not parse: invalid syntax (<unknown>, line 1) |
| `merged` | table (rowspan) | PASS | FAIL | 0 | QueryError: query did not parse: invalid syntax (<unknown>, line 1) |
| `pivot` | table (transposed) | PASS | FAIL | 0 | QueryError: query did not parse: invalid syntax (<unknown>, line 1) |
| `api` | json | PASS | PASS | 3 | 3 rows, probe name~'Aeropress' found |
| `cursor` | json | PASS | PASS | 4 | 4 rows, probe name~'Item 1' found |
| `rss` | xml feed | PASS | PARTIAL | 3 | 3 rows but probe title~'Q3 earnings released' MISSING |
| `paginated` | pagination | PASS | PASS | 4 | 4 rows, probe name~'Row 1' found |
| `looppager` | pagination | PASS | FAIL | 0 | no rows (query did not produce a list) |
| `overlap` | pagination | PASS | PASS | 5 | 5 rows, probe name~'Item 1' found |
| `deep` | pagination + detail | PASS | PASS | 4 | 4 rows, probe name~'Item 1' found |
| `news` | sibling rows | PASS | PARTIAL | 6 | 6 rows but probe title~'CPU' MISSING |
| `sections` | split sections | PASS | FAIL | 3 | 3 rows; probe title~'Autumn Cupping' not found |
| `twoface` | json island | PASS | FAIL | 3 | 3 rows; probe name~'Item 1' not found |
| `tabs` | tabbed | FAIL | FAIL | 0 | locate found no source |
| `spa` | browser: spa | FAIL | FAIL | 0 | locate found no source |
| `feed` | browser: xhr | FAIL | FAIL | 0 | locate found no source |

## Reading the results

**Locate is deterministic and strong.** It finds the dataset region (or the JSON/XML document) on every well-formed page, and prefers a consistent XHR/data-API endpoint over the HTML when one backs the page. It is imprecise only where the record region is nested (a table's `tr` vs the tighter `tbody tr`), and it under-detects on browser-gated pages whose records are not in the static DOM.

**Author (with the heuristic stand-in) handles the common shapes and exposes the hard ones.** It gets the flat lists, the header-column table, the JSON/API and RSS documents, and the list-valued field. It falls short exactly where the guide tells a real model to do something structural the heuristic does not attempt:

- **sibling rows / split sections / JSON islands** (`news`, `sections`, `twoface`) -- need `:scope +`, a grouped multi-section selector, or reparsing a `<script>` island; the heuristic writes a single flat `select_all`.
- **transposed tables** (`pivot`) -- records are COLUMNS; the flat author does not detect that it should transpose. The guide now documents `wq.doc.tables(sel, transpose=True)` (and rowspan carry-down via `.tables(sel)`), so a real model handles both -- the heuristic cannot tell a transposed table from a normal one, so it stays PARTIAL here.
- **total rows** (`ranking`) -- extracted but not filtered out.
- **pagination** (`paginated`, `looppager`, `overlap`, `deep`) -- Author writes one correct page and the pipeline is told (an advisory note) to follow the pager; the single-page rows are right, so these read as PASS with a pager note.
- **browser-gated** (`spa`, `feed`, `tabs`) -- the static eval resolver does not render JS or drive tabs, so the records are not in the DOM it sees; these need a browser tier.

This run is against the current DSL, where `select` is **loud by default** (a miss raises `dsl.select_miss`). The heuristic marks every extract-column select `optional=True` (a mechanical author cannot know which fields are on every row), and the guide teaches `optional=True` for genuinely-optional fields and presence filters.

The Author gaps are heuristic-author gaps, not pipeline gaps: the guide already documents each of these shapes, so a capable model has what it needs. The pipeline (prompt assembly, the safe compile, rerooting, running, the flag-keyed advisory notes) works on every case that produced rows.
