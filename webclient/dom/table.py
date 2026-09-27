"""HTML table normalisation: expand ``rowspan`` / ``colspan`` into a dense grid, then read the grid
as records -- one per data ROW (keyed by the header row) or, TRANSPOSED, one per data COLUMN (keyed by
the first column). This is what makes a table with MERGED cells (a category cell that spans several
rows) or a PIVOTED layout (a feature-comparison matrix whose records are columns) extractable: the
``select`` primitive reads within one element and cannot carry a spanning cell across sibling rows.

The op that uses this (``Document.table``) is EXPLICIT -- nothing here runs unless the query asks for
it, so ``select_all`` keeps its plain behaviour."""

from __future__ import annotations

from typing import Any

from .parse import tag, text_of


def _cells(tr: Any) -> "list[Any]":
    """The ``<td>`` / ``<th>`` cells of a row, in document order."""
    return [c for c in tr if tag(c) in ("td", "th")]


def _own_rows(table: Any) -> "list[Any]":
    """The ``<tr>`` rows that belong to THIS table, in document order -- rows inside a NESTED
    table (a table within a cell) belong to that inner table, not this one, so their subtree is
    pruned. ``table.iter()`` would wrongly count a nested table's rows as rows of the outer one."""
    out: list[Any] = []

    def walk(el: Any) -> None:
        for child in el:
            t = tag(child)
            if t == "table" and child is not table:  # a nested table: its rows are its own
                continue
            if t == "tr":
                out.append(child)
            walk(child)

    walk(table)
    return out


def dense_grid(table: Any) -> "list[list[str]]":
    """The table as a DENSE rectangular grid of cell texts, with ``rowspan`` / ``colspan`` EXPANDED:
    a cell that spans N rows / M columns fills all N x M grid positions with its text, so every logical
    row has a value in every column (a merged category cell is carried DOWN into the rows it covers).
    Rows are every ``<tr>`` under the table (``thead`` + ``tbody``), in order."""
    rows = _own_rows(table)
    grid: list[list[str]] = []
    # cells still spanning DOWN into later rows: col -> [text, rows_remaining]
    pending: dict[int, list[Any]] = {}
    for tr in rows:
        row: dict[int, str] = {}
        # first, lay down the cells spanning into this row from above
        for col in list(pending):
            text, left = pending[col]
            row[col] = text
            pending[col][1] = left - 1
            if pending[col][1] <= 0:
                del pending[col]
        col = 0
        for cell in _cells(tr):
            while col in row:  # skip columns already filled by a spanning cell
                col += 1
            try:
                colspan = max(1, int(cell.get("colspan") or 1))
                rowspan = max(1, int(cell.get("rowspan") or 1))
            except (TypeError, ValueError):
                colspan = rowspan = 1
            text = text_of(cell)
            for j in range(colspan):
                row[col + j] = text
                if rowspan > 1:  # remember to fill this column in the next rowspan-1 rows
                    pending[col + j] = [text, rowspan - 1]
            col += colspan
        width = (max(row) + 1) if row else 0
        grid.append([row.get(c, "") for c in range(width)])
    if not grid:
        return []
    width = max(len(r) for r in grid)  # pad ragged rows so the grid is rectangular
    return [r + [""] * (width - len(r)) for r in grid]


def _uniq_headers(labels: "list[str]") -> "list[str]":
    """Header labels made unique + non-empty (``""`` -> ``col1``; a repeat -> ``label_2``), so they are
    safe dict keys."""
    out: list[str] = []
    seen: dict[str, int] = {}
    for i, lab in enumerate(labels):
        key = lab.strip() or f"col{i + 1}"
        if key in seen:
            seen[key] += 1
            key = f"{key}_{seen[key]}"
        else:
            seen[key] = 1
        out.append(key)
    return out


def _header_row(grid: "list[list[str]]") -> int:
    """Index of the row to read as the column header. A leading row whose cells are ALL identical is
    a full-width spanning title / navbar / section-caption row (one cell with ``colspan`` = the whole
    width), not the column header -- skip such rows until one with distinct cells is found. Falls back
    to row 0 (e.g. a genuinely single-column table, where every row is trivially 'all identical')."""
    for i, row in enumerate(grid):
        if row and len(set(row)) > 1:
            return i
    return 0


def table_records(table: Any, *, transpose: bool = False) -> "list[dict[str, str]]":
    """The dense grid read as records. Normal: the header row (the first non-spanning row) keys each
    later row as a record ``{header: cell}``. ``transpose``: the FIRST column is the header (its labels
    are the keys) and each later COLUMN is a record -- for a feature-comparison / named-feature matrix
    whose records are columns. Empty when the table has no data rows/columns beyond the header."""
    grid = dense_grid(table)
    if len(grid) < 2 or not grid[0]:
        return []
    if not transpose:
        h = _header_row(grid)
        headers = _uniq_headers(grid[h])
        return [{headers[j]: (r[j] if j < len(r) else "") for j in range(len(headers))} for r in grid[h + 1:]]
    # transpose: labels are column 0; each further column is a record keyed by those labels
    labels = _uniq_headers([r[0] if r else "" for r in grid])
    ncols = max(len(r) for r in grid)
    return [{labels[i]: (grid[i][c] if c < len(grid[i]) else "") for i in range(len(grid))}
            for c in range(1, ncols)]
