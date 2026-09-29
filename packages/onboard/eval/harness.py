"""The onboard eval: run Locate + Author against the webclient LAB and grade each example.

For every case below we run the two phases against a live lab fixture:

* **Locate** -- does :func:`web.onboard.locate` find the right source (the expected record
  selector, or a JSON/XML data document, preferring an XHR/data-API when one backs the page)?
* **Author** -- does :func:`web.onboard.author` produce a ``wq`` query that, run, extracts the
  intended rows? Author is driven here by :class:`~eval.heuristic_llm.HeuristicLlm` (no API key), so
  this grades the PIPELINE MECHANICS + a mechanical author, not a model's selector quality.

Each case is graded PASS / PARTIAL / FAIL against the fixture's published expected result, so a
regression -- or a genuine capability gap -- is explicit. ``__main__`` serves the lab, runs this,
and writes ``RESULTS.md``.
"""

from __future__ import annotations

from dataclasses import dataclass

from web.onboard import DatasetBrief, LocateBrief, Reference, build_query, locate

from web.resolve import Resolver

from .heuristic_llm import HeuristicLlm


@dataclass
class Case:
    """One lab example to evaluate. ``category`` groups the report; ``sel`` is the record selector
    Locate should find (``None`` for a JSON/XML doc); ``kind`` the expected document kind; ``rows``
    the expected row count on ONE static fetch (per-page for a paginated list); ``probe`` a
    ``(field, value)`` a correct extraction must yield in some row."""

    name: str
    category: str
    fields: list[str]
    sel: "str | None"
    kind: str
    rows: int
    probe: "tuple[str, str]"
    note: str = ""
    paginated: bool = False


@dataclass
class Result:
    name: str
    category: str
    located: bool
    locate_grade: str
    author_grade: str
    n_rows: int
    engine: str
    reply: str
    detail: str = ""


CASES: list[Case] = [
    # -- static record lists: the bread-and-butter dataset shapes ---------------------------------
    Case(
        "shop",
        "static list",
        ["title", "price", "url"],
        "div.card",
        "html",
        3,
        ("title", "Aeropress"),
    ),
    Case(
        "store",
        "static list",
        ["name", "price"],
        "li.product",
        "html",
        5,
        ("name", "Ethiopia Yirgacheffe"),
    ),
    Case(
        "board",
        "static list",
        ["title"],
        "li.post",
        "html",
        5,
        ("title", "Senior Engineer"),
    ),
    Case("frozen", "static list", ["name"], "article.item", "html", 3, ("name", "Row A")),
    Case("large", "static list", ["k", "v"], "li.item", "html", 6000, ("v", "value 0")),
    Case(
        "catalog",
        "static list",
        ["title", "rating", "price"],
        "article.product_pod",
        "html",
        6,
        ("title", "A Light in the Attic"),
    ),
    Case(
        "quotes",
        "list + list-field",
        ["text", "author", "tags"],
        "div.quote",
        "html",
        6,
        ("author", "Albert Einstein"),
        note="tags is a list-valued field",
    ),
    # -- tables ------------------------------------------------------------------------------------
    Case(
        "table",
        "table",
        ["item", "price", "stock"],
        "tbody tr",
        "html",
        3,
        ("item", "Aeropress"),
    ),
    Case(
        "ranking",
        "table",
        ["country", "population"],
        "table.rank tbody tr",
        "html",
        3,
        ("population", "1412"),
        note="has a total row the author does not drop",
    ),
    Case(
        "merged",
        "table (rowspan)",
        ["category", "item", "price"],
        "tbody tr",
        "html",
        5,
        ("category", "Fruit"),
        note="rowspan category is not carried down",
    ),
    Case(
        "pivot",
        "table (transposed)",
        ["plan", "price", "users"],
        "tbody tr",
        "html",
        3,
        ("plan", "Starter"),
        note="records are COLUMNS -- needs a transpose the author lacks",
    ),
    # -- JSON / XML documents ----------------------------------------------------------------------
    Case(
        "api",
        "json",
        ["name", "value", "currency"],
        None,
        "json",
        3,
        ("name", "Aeropress"),
    ),
    Case(
        "cursor",
        "json",
        ["name"],
        None,
        "json",
        4,
        ("name", "Item 1"),
        note="cursor pager",
    ),
    Case(
        "rss",
        "xml feed",
        ["title", "date"],
        None,
        "xml",
        3,
        ("title", "Q3 earnings released"),
    ),
    # -- pagination (author writes ONE page; a note flags the pager) -------------------------------
    Case(
        "paginated",
        "pagination",
        ["name"],
        "article.row",
        "html",
        4,
        ("name", "Row 1"),
        paginated=True,
    ),
    Case(
        "looppager",
        "pagination",
        ["name"],
        "article.row",
        "html",
        4,
        ("name", "Item 1"),
        paginated=True,
    ),
    Case(
        "overlap",
        "pagination",
        ["name"],
        "li.item",
        "html",
        3,
        ("name", "Item 1"),
        paginated=True,
        note="overlapping pages need dedup across the pager",
    ),
    Case(
        "deep",
        "pagination + detail",
        ["name"],
        "article.item",
        "html",
        4,
        ("name", "Item 1"),
        paginated=True,
        note="sku lives on the detail page (per-record resolve)",
    ),
    # -- harder extraction shapes ------------------------------------------------------------------
    Case(
        "news",
        "sibling rows",
        ["title", "points", "user", "age"],
        "tr.athing",
        "html",
        6,
        ("title", "CPU"),
        note="record + data split across sibling <tr>s",
    ),
    Case(
        "sections",
        "split sections",
        ["title", "date"],
        "li.past",
        "html",
        4,
        ("title", "Autumn Cupping"),
        note="two differently-shaped sections in one dataset",
    ),
    Case(
        "twoface",
        "json island",
        ["name"],
        "article.card",
        "html",
        12,
        ("name", "Item 1"),
        note="full data is in a <script ld+json> island, not the visible teasers",
    ),
    # -- interaction / browser-gated (the static eval resolver does not render) --------------------
    Case(
        "tabs",
        "tabbed",
        ["title"],
        "li.event",
        "html",
        4,
        ("title", "Autumn Cupping"),
        note="records behind tabs; static fetch sees the first tab only",
    ),
    Case(
        "spa",
        "browser: spa",
        ["title"],
        "div.card",
        "html",
        3,
        ("title", "Item 1"),
        note="JS-rendered; needs a browser tier",
    ),
    Case(
        "feed",
        "browser: xhr",
        ["title"],
        None,
        "json",
        3,
        ("title", "Item 1"),
        note="records arrive via XHR; needs a browser/xhr tier",
    ),
]


def _grade_locate(case: Case, ref: "Reference | None") -> str:
    if ref is None:
        return "FAIL"
    if case.sel is None:  # a JSON/XML document source
        return "PASS" if ref.kind == case.kind else "PARTIAL"
    got = ref.record_selector or ""
    if got == case.sel or case.sel.endswith(got) and got:
        return "PASS"
    return "PARTIAL" if got else "FAIL"


def _grade_author(case: Case, rows: "list[dict[str, object]] | None") -> "tuple[str, str]":
    if not isinstance(rows, list):
        return "FAIL", "no rows (query did not produce a list)"
    key, want = case.probe
    hit = any(want.lower() in str(r.get(key, "")).lower() for r in rows)
    enough = len(rows) >= case.rows or case.paginated  # a paginated list authors ONE page
    if hit and enough:
        return "PASS", f"{len(rows)} rows, probe {key}~{want!r} found"
    if hit:
        return (
            "PARTIAL",
            f"{len(rows)} rows (expected >= {case.rows}); probe found but count short",
        )
    if len(rows) >= case.rows:
        return "PARTIAL", f"{len(rows)} rows but probe {key}~{want!r} MISSING"
    return "FAIL", f"{len(rows)} rows; probe {key}~{want!r} not found"


async def run_case(case: Case, base: str, resolver: Resolver) -> Result:
    """Run Locate then Author for one case against the live lab and grade both phases."""
    url = f"{base}/lab/{case.name}"
    llm = HeuristicLlm()
    ref = await locate(LocateBrief(goal=case.name, candidates=[url]), resolver=resolver)
    locate_grade = _grade_locate(case, ref)
    if ref is None:
        return Result(
            case.name,
            case.category,
            False,
            locate_grade,
            "FAIL",
            0,
            "-",
            "",
            "locate found no source",
        )
    brief = DatasetBrief(goal=case.name, fields=case.fields)
    try:
        query, engine, _notes = await build_query(ref, brief, resolver=resolver, llm=llm)
        rows = await query.acollect(resolver=resolver)
    except Exception as exc:  # a genuine author/run failure is a FAIL, not a crash
        return Result(
            case.name,
            case.category,
            True,
            locate_grade,
            "FAIL",
            0,
            "-",
            llm.reply,
            f"{type(exc).__name__}: {exc}",
        )
    row_list = rows if isinstance(rows, list) else None
    grade, detail = _grade_author(case, row_list)
    return Result(
        case.name,
        case.category,
        True,
        locate_grade,
        grade,
        len(row_list) if row_list else 0,
        engine,
        llm.reply,
        detail,
    )


async def run_all(base: str) -> list[Result]:
    async with Resolver() as resolver:
        return [await run_case(c, base, resolver) for c in CASES]


__all__ = ["Case", "Result", "CASES", "run_case", "run_all"]
