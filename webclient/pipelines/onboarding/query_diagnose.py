"""onboarding.query_diagnose -- run an authored query and ASSESS it, and build the model feedback.

Interpret a collect() result into data rows, run the query under a hard timeout, validate the
required fields, and turn a miss into an actionable, targeted retry hint (wrong record selector /
empty fields / a value that lives in an attribute / a section that matched nothing / stale data)."""

from typing import Any

from ...interface import wq
from .common import log
from .dates import _dig
from .artifacts import Brief, QueryArtifact


def _data_rows(result: Any) -> list[Any]:
    """The EXTRACTED DATA a query produced -- plain rows (dicts / scalars), never the
    elements themselves. A query that stops at ``select_all`` yields a collection of
    ``Document`` elements: that is a selection, NOT extracted data, so it counts as
    zero rows (the author must ``.project()``). This is what stops an un-projected
    query from looking like a success and what keeps the sample as data, not objects."""
    from ...core.web_core import WebCore

    if result is None or isinstance(result, WebCore):
        return []  # None, or a single selected element -- not data
    try:
        items = list(result)
    except TypeError:  # a scalar / Field -- one value
        return [result]
    if items and all(isinstance(i, WebCore) for i in items):
        return []  # a collection of elements -- selected, not extracted (no project)
    return items

def _row_selector(expr: Any) -> "str | None":
    """The CSS/selector string the query selects its repeating record with -- the argument
    of the first ``select``/``select_all``. Used to diagnose a 0-row query: if this
    selector matches nothing on the page, the row selector itself is wrong."""
    steps = list(expr._plan.steps)
    for i, s in enumerate(steps):
        if s.kind == "get" and s.name in ("select", "select_all"):
            nxt = steps[i + 1] if i + 1 < len(steps) else None
            if nxt is not None and nxt.kind == "call" and nxt.args:
                return getattr(nxt.args[0], "value", None)
    return None

def _selector_match_count(sel: "str | None", doc: Any) -> "int | None":
    """How many elements ``sel`` matches on the fetched ``doc`` (``None`` if it can't be
    probed). Lets the feedback tell the model whether its ROW selector is wrong (0 matches)
    or whether the record matches but the FIELD extraction is (matches, but no data)."""
    if not sel or not doc.ok:
        return None
    try:
        probe = wq.doc.select_all(sel).extract(_=wq.doc.attr("text")).project()
        return len(_data_rows(probe.collect(doc)))
    except Exception:  # noqa: BLE001 - a selector the engine can't run -> unknown
        return None

def _no_rows_hint(expr: Any, doc: Any) -> str:
    """A human-readable, actionable hint for why a query extracted 0 rows: either the ROW
    selector matched nothing (wrong record selector) or it matched records but no fields
    came out (wrong field selectors / missing ``.project()``). Guides the retry."""
    sel = _row_selector(expr)
    n = _selector_match_count(sel, doc)
    ops = {s.name for s in expr._plan.steps if s.kind == "get"}
    projected = "project" in ops  # did the query actually extract+project fields?
    if n == 0:
        return (
            f'Your record selector "{sel}" matched NO elements on this page, so nothing was'
            " extracted. Look again at the skeleton and pick a selector that matches ONE"
            " element per record (a repeated tag/class you can see in the skeleton)."
        )
    if n and not projected:
        # the record selector matched, but the query never extracted -- the classic "selected
        # elements, forgot to pull fields" -- so it produced 0 DATA rows.
        return (
            f'Your record selector "{sel}" matched {n} record(s), but your query only SELECTED'
            " them -- it never extracted fields, so it produced 0 data rows. Add"
            " .extract(col=wq.doc.select(...).attr(...), ...) for each field and END with"
            " .project()."
        )
    if n:
        return (
            f'Your record selector "{sel}" matched {n} element(s), but NONE of your field'
            " selectors found a value inside them. Two likely causes: (1) the record selector is"
            " too broad -- it matched a WRAPPER, not one record each. Prefer a SEMANTIC anchor: a"
            ' repeated <article>/<li>/<tr>, or [class*="product"]/[class*="post"] describing the'
            ' record -- NOT a hashed build class (e.g. ".AMTIxG_grid", ".css-1a2b3c"), which names'
            " a styling box, not a record. (2) your field selectors are right relative to the"
            " record but match nothing in it. Use the record HTML below: pick the element that"
            " wraps exactly ONE record, then field selectors you can SEE inside it."
        )
    return (
        "Your query ran but extracted 0 data rows: it MUST .select_all(<record selector>),"
        " pull each field with .extract(col=...), and END with .project() so it returns"
        " data rows -- not selected elements. Re-check your selectors against the skeleton."
    )

def _sample_record_html(expr: Any, doc: Any, *, limit: int = 900) -> str:
    """The FIRST matched record's own markup (whitespace-collapsed, trimmed) -- so a retry
    hint can SHOW the model the exact element it must extract from. This is what turns a
    vague "a field is empty" into a fixable one: the record's HTML reveals values that live
    in an ATTRIBUTE (``data-rating="Four"``, ``datetime=...``) rather than in text, so the
    model can switch ``.attr("text")`` to ``.attr("data-rating")``. Empty if unprobeable."""
    sel = _row_selector(expr)
    if not sel or not doc.ok:
        return ""
    try:
        first = wq.doc.select(sel).collect(doc)  # the first matching record element
        html = first.html() if getattr(first, "ok", False) else ""
    except Exception:  # noqa: BLE001 - a selector the engine can't run -> no sample
        return ""
    html = " ".join(html.split())
    return html[:limit] + ("…" if len(html) > limit else "")

def _nonempty(v: Any) -> bool:
    """Whether an extracted value actually carries content -- not ``None``, not blank/
    whitespace, not an empty list/dict. The test of "did the selector match content"."""
    if v is None:
        return False
    if isinstance(v, str):
        return v.strip() != ""
    if isinstance(v, (list, dict, tuple, set)):
        return len(v) > 0
    return True

def _populated_rows(rows: "list[Any]") -> "list[Any]":
    """The rows that carry AT LEAST ONE non-empty field. A row of all-empty cells means
    the record selector matched an element but every FIELD selector matched nothing (a
    guessed query, or content that isn't in this HTML) -- it is not real extracted data,
    so it must not count as a extracted row."""
    out: list[Any] = []
    for r in rows:
        if isinstance(r, dict):
            if any(_nonempty(v) for v in r.values()):
                out.append(r)
        elif _nonempty(r):
            out.append(r)
    return out

def _required_leaf_paths(brief: Brief) -> "list[str]":
    """The required LEAF field paths (dotted). A leaf is a field with no deeper field under
    it (``price.value`` / ``price.unit`` are leaves; ``price`` is their branch). A leaf is
    optional if IT or any ancestor is marked optional. Used to validate nested extractions:
    a query that produced ``price = {"value":"","unit":""}`` populated the branch but NONE of
    its required leaves, which the old top-level check missed."""
    fields = list(brief.fields)
    opt = set(brief.optional)
    leaves = [f for f in fields if not any(g != f and g.startswith(f + ".") for g in fields)]
    req: list[str] = []
    for leaf in leaves:
        parts = leaf.split(".")
        ancestors = [".".join(parts[: i + 1]) for i in range(len(parts))]
        if not any(a in opt for a in ancestors):  # leaf or an ancestor optional -> skip
            req.append(leaf)
    return req

def _empty_required_fields(rows: "list[Any]", brief: Brief) -> "list[str]":
    """Required LEAF paths that are EMPTY (or absent) across every row -- their selectors
    matched no content, so the query is only a partial guess. Recurses into nested rows via
    :func:`_dig`, so an empty nested leaf (``price.value``) is caught even when its branch dict
    is present. Empty when the rows carry every required leaf; skipped with no schema."""
    req = _required_leaf_paths(brief)
    dict_rows = [r for r in rows if isinstance(r, dict)]
    if not req or not dict_rows:
        return []
    return [p for p in req if not any(_nonempty(_dig(r, p)) for r in dict_rows)]

def _content_hint(expr: Any, rows: "list[Any]", brief: Brief, doc: Any) -> str:
    """The retry hint when a query RAN but did not truly extract the dataset -- naming the
    specific validation that failed (record selector matched nothing / matched but fields
    are empty / a required field is empty) and warning that the content may not be in the
    HTML at all (client-rendered / iframe / shadow DOM), which a static query can't reach."""
    caveat = (
        " If the records are not visible in the skeleton at all, the page is likely rendered"
        " client-side (an SPA) or the data sits inside an iframe or shadow DOM -- a static"
        " query cannot reach it; do NOT guess selectors that are not in the skeleton."
    )
    sample = _sample_record_html(expr, doc)
    shown = (
        f"\n\nHere is the FIRST matched record's HTML -- find the missing field(s) IN IT. A "
        f"value may live in an ATTRIBUTE (e.g. data-rating=\"Four\", datetime=\"...\") rather "
        f"than in the element text: read it with .attr(\"<name>\"), not .attr(\"text\"). Do not "
        f"add fields that are genuinely absent here.\n{sample}"
        if sample else ""
    )
    if not _populated_rows(rows):  # matched a container but every field is empty (or 0 rows)
        return _no_rows_hint(expr, doc) + shown + caveat
    empty = _empty_required_fields(rows, brief)  # some required field never came out
    cols = ", ".join(f'"{c}"' for c in empty)
    return (
        f"Your query extracted rows, but the required field(s) {cols} were EMPTY on every"
        " row. Either their selector matched no element, OR it matched an element whose TEXT"
        " is empty because the value is in an attribute (use .attr(\"<name>\"))." + shown + caveat
    )

#: hard wall-clock cap on running ONE authored query against the source. A pathological LLM
#: query (a per-record .resolve() fanning out to hundreds of fetches, a resolve to a slow host)
#: must never hang the pipeline: the test is cancelled at this bound and counts as a failure.
_QUERY_TEST_TIMEOUT = 45.0

def _test_query(expr: Any, doc: Any, *, timeout: float = _QUERY_TEST_TIMEOUT) -> "tuple[bool, list[Any]]":
    """Run the authored query against the fetched source ``doc`` to prove it loads and
    actually EXTRACTS the dataset. The query is the document-level extraction
    (``wq.doc...``), so it collects directly against the resolved document. Returns
    ``(ran_without_error, rows)`` -- a query that only selects elements (no ``.project()``)
    extracts zero rows and so is not treated as a working query.

    HARD-BOUNDED: the run is cancelled after ``timeout`` seconds (``asyncio.wait_for`` cancels
    the coroutine, so in-flight fetches stop), so no authored query can hang the pipeline."""
    import asyncio

    loop = getattr(getattr(doc, "_client", None), "loop", None)
    try:
        if callable(loop):  # bound + cancel on the engine loop (the normal path)
            async def _bounded() -> Any:
                return await asyncio.wait_for(expr.acollect(doc), timeout=timeout)

            engine: Any = loop()
            result = engine.run(_bounded())
        else:  # no engine loop (an unbound doc) -- fall back to the plain sync collect
            result = expr.collect(doc)
    except (asyncio.TimeoutError, TimeoutError):
        log.warning("    query test exceeded %.0fs and was cancelled -- treated as a FAILED "
                    "attempt (a query must not hang; avoid per-record .resolve() over many rows)",
                    timeout)
        return False, []
    except Exception:  # noqa: BLE001 - a query that can't run against the source
        return False, []
    return True, _data_rows(result)

def _short_fail_reason(expr: Any, rows: "list[Any]", brief: Brief, doc: Any) -> str:
    """A ONE-LINE reason a query didn't extract cleanly -- for the log (the full, multi-line
    hint goes to the model, not the console). Keeps the retry trace legible."""
    good = _populated_rows(rows)
    if not good:
        n = _selector_match_count(_row_selector(expr), doc)
        return f"0 rows (record selector matched {n if n is not None else '?'})"
    empty = _empty_required_fields(good, brief)
    if empty:
        return f"required field(s) {', '.join(empty)} empty on every row"
    return f"{len(good)} row(s) but incomplete"

def _resolve_on_non_link(expr: Any) -> "str | None":
    """Detect the common model mistake of calling ``.resolve()`` on a VALUE rather than a link
    (``.attr("text").resolve()`` instead of ``.attr("href").resolve()``). Returns a targeted
    feedback message, or ``None`` if every resolve follows a link attr. Recurses into nested
    sub-plans (``extract`` columns, ``when`` branches). ``resolve`` on a root reference /
    ``reference(col)`` (no preceding attr) is fine."""

    def _msg(name: str) -> str:
        return (
            f'You called .resolve() on .attr("{name}") -- but .resolve() follows a LINK, and a '
            'value/text is not a URL. To READ a value, .attr("text") is the whole answer (do not '
            'resolve it). To FOLLOW a link, resolve its href: .select("a").attr("href").resolve(). '
            'Re-write the query.'
        )

    def scan(steps: "list[Any]") -> "str | None":
        last_attr: "str | None" = None  # the arg of the most recent attr(...) call, per (sub)plan
        for i, s in enumerate(steps):
            if s.kind == "get" and s.name == "attr":
                nxt = steps[i + 1] if i + 1 < len(steps) else None
                if nxt is not None and nxt.kind == "call" and nxt.args:
                    v = nxt.args[0].value
                    last_attr = v if isinstance(v, str) else None
            elif s.kind == "get" and s.name == "resolve":
                if last_attr is not None and last_attr not in ("href", "src", "action"):
                    return _msg(last_attr)
                last_attr = None  # a valid resolve on a link (or a root/reference resolve)
            elif s.kind == "get" and s.name in ("select", "select_all", "reference"):
                last_attr = None  # a new selection -> the previous attr no longer applies
            if s.kind == "call":  # recurse into sub-plan args/kwargs (extract cols / when branches)
                for a in [*s.args, *s.kwargs.values()]:
                    sub = getattr(a, "plan", None)
                    if sub is not None and (r := scan(list(sub.steps))) is not None:
                        return r
        return None

    return scan(list(getattr(expr._plan, "steps", [])))

def _precheck_sections(exprs: "list[Any]") -> "tuple[str, str] | None":
    """The pre-test guards applied to EVERY section query before it's run: each must actually
    EXTRACT something and none may call ``.resolve()`` on a value instead of a link. A repeating
    dataset selects rows with ``.select_all(...).extract(...).project()``; but a GENERIC dataset need
    not be a repeating list -- a SINGLE record (``wq.doc.extract(...).project()``, one row) or a lone
    value (``wq.doc.select(...).attr(...)``) is valid too. So the guard rejects only a query that
    extracts NOTHING (no ``select``/``select_all``, and no ``extract``/``project``/value read).
    Returns ``(reason, follow_up)`` for the FIRST offending section, or ``None`` when all pass."""
    #: ops that read a value out (so the query yields data even without a repeating .select_all).
    reads = {"extract", "project", "attr", "text", "markdown", "html", "links", "table", "download"}
    multi = len(exprs) > 1
    for i, expr in enumerate(exprs):
        where = f"section {i + 1} " if multi else ""
        ops = {s.name for s in expr._plan.steps if s.kind == "get"}
        if not (({"select", "select_all"} & ops) or (reads & ops)):
            return (
                f"{where}extracts nothing (no selection or field read)".strip(),
                f"{'Section ' + str(i + 1) + ' of your reply' if multi else 'Your query'} does NOT"
                " extract anything. For a REPEATING dataset select the record with .select_all(...),"
                " pull each field with .extract(col=...), and END with .project(). For a SINGLE record"
                " or a lone value, wq.doc.extract(col=...).project() (one row) or wq.doc.select(...)"
                ".attr(...) is fine. Re-write it.",
            )
        bad = _resolve_on_non_link(expr)
        if bad is not None:
            return (f"{where}.resolve() called on a value, not a link".strip(), bad)
    return None

def _split_section_follow_up(empties: "list[int]", exprs: "list[Any]") -> str:
    """Feedback for a SPLIT query that ran but didn't fully land. Names the section(s) that
    matched 0 records (fix the selector, or keep it if that section is genuinely empty) or, when
    all sections have rows but the result is still incomplete, asks for more semantic record
    selectors. Always restates the ``---``-separated one-query-per-section contract."""
    contract = (" Reply with one wq.doc... chain per section, separated by a line containing"
                " only ---.")
    if empties:
        which = ", ".join(str(i) for i in empties)
        return (
            f"Your split query ran, but section(s) {which} matched 0 records. Fix that section's"
            " .select_all(...) record selector to match its rows. If that section is GENUINELY"
            " empty on the page (e.g. no upcoming events), keep it as is and leave the working"
            " sections unchanged." + contract
        )
    return ("Your split query did not extract the dataset. Re-write each section's .select_all(...)"
            " to a more semantic record selector -- don't just tweak the fields." + contract)

def _recency_follow_up(art: QueryArtifact) -> str:
    """The feedback that pushes the model toward the MOST RECENT data + the hidden-tabs pattern."""
    return (
        f"That query is COMPLETE, but the most recent data looks MISSING: {art.timeliness}. "
        "The records you selected are probably an ARCHIVED period. Sites keep the CURRENT period "
        "behind a control: YEAR TABS (an older year shows by default), a 'Latest' vs 'Archive' "
        "toggle, a category filter, or a paginated first page. In the skeleton look for clickable "
        "tab/filter controls (marked '← clickable'), pagination, and records marked '[xhr]' "
        "(loaded on demand). Re-write the query to capture the MOST RECENT records -- pick a "
        "record selector that is NOT scoped to one archived tab (select across all of them, or "
        "the current/latest tab). Is there more recent data than what you selected?"
    )

def _should_retry_for_recency(art: QueryArtifact, attempt: int, tries: int, already_tried: bool = False) -> bool:
    """A COMPLETE query whose newest data looks stale MIGHT be scoped to an ARCHIVED period (a
    hidden year tab, an old paginated page) -- worth ONE more try for fresher data. But stale is a
    FLAG, not a ship blocker: when the retry didn't find anything newer (``already_tried``), or the
    dataset simply IS older than its cadence (a low-frequency or fixed feed), we keep the working
    query. So: retry at most once, and only while attempts remain."""
    return art.stale and not already_tried and attempt < tries - 1
