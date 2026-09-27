"""onboarding.query_diagnose -- run an authored query and ASSESS it, and build the model feedback.

Interpret a collect() result into data rows, run the query under a hard timeout, validate the
required fields, and turn a miss into an actionable, targeted retry hint (wrong record selector /
empty fields / a value that lives in an attribute / a section that matched nothing / stale data)."""

import json
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

def _force_fields_optional(expr: Any) -> "Any | None":
    """A copy of the query with every FIELD ``select``/``select_all`` made ``optional=True`` -- so a
    field selector that MISSES on some records yields null there instead of RAISING (a non-optional
    miss in a fan-out zeroes the WHOLE query). Returns None if there is nothing to relax."""
    import copy

    from ...query.expr import Expr
    from ...query.plan import Plan

    plan = copy.deepcopy(expr._plan.model_dump(mode="json"))
    changed = [False]

    def walk(p: "dict[str, Any]", *, in_field: bool) -> None:
        steps = p.get("steps", [])
        for i, s in enumerate(steps):
            if in_field and s.get("kind") == "get" and s.get("name") in ("select", "select_all"):
                nxt = steps[i + 1] if i + 1 < len(steps) else None
                if nxt is not None and nxt.get("kind") == "call":
                    kw = nxt.setdefault("kwargs", {})
                    if "optional" not in kw:
                        kw["optional"] = {"value": True}
                        changed[0] = True
            if s.get("kind") == "call":  # descend into the field sub-plans
                for v in list(s.get("kwargs", {}).values()) + list(s.get("args", [])):
                    if isinstance(v, dict) and isinstance(v.get("plan"), dict):
                        walk(v["plan"], in_field=True)

    walk(plan, in_field=False)
    if not changed[0]:
        return None
    return Expr(Plan.model_validate(plan), expr._client)

def _required_field_raises(expr: Any, doc: Any) -> "str | None":
    """Detect the case where the RECORD selector matches records and the field selectors work on
    MOST of them, but one field selector matches nothing on SOME record (a leading summary/total
    row, a header row) and -- not being optional -- RAISES, zeroing the whole query. Returns the
    failing selector's message (for the hint) when that is what happened, else None. Confirmed by
    re-testing with every field forced optional: if THAT extracts rows, a required field was the
    culprit."""
    if not doc.ok:
        return None
    try:
        expr.collect(doc)
        return None  # it did not raise -> this is not the missing-required-field case
    except Exception as exc:  # noqa: BLE001 - we only want the message, and only if optional fixes it
        msg = str(exc).splitlines()[0][:160]
    opt = _force_fields_optional(expr)
    if opt is None:
        return None
    ok, rows = _test_query(opt, doc)
    return msg if (ok and _populated_rows(rows)) else None

def _no_rows_hint(expr: Any, doc: Any) -> str:
    """A human-readable, actionable hint for why a query extracted 0 rows: either the ROW
    selector matched nothing (wrong record selector) or it matched records but no fields
    came out (wrong field selectors / missing ``.project()``). Guides the retry."""
    sel = _row_selector(expr)
    n = _selector_match_count(sel, doc)
    ops = {s.name for s in expr._plan.steps if s.kind == "get"}
    projected = "project" in ops  # did the query actually extract+project fields?
    raised = _required_field_raises(expr, doc)  # a required field missing on SOME rows -> whole query raises
    if raised is not None:
        return (
            f'Your record selector "{sel}" matched {n} records and your field selectors DO work on '
            "MOST of them -- but at least one field selector matches NOTHING on SOME record (a "
            'leading summary/total row like "World", a section header, an ad row), and because that '
            f"field is NOT optional the WHOLE query raises ({raised}) and returns 0 rows. Add "
            'optional=True to the field selector(s) that can be absent on some records: '
            'wq.doc.select("<selector>", optional=True).attr("text"). An optional field yields null '
            "on the rows that lack it instead of failing the entire query."
        )
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

def _looks_like_json_blob(v: Any) -> bool:
    """A value that is a STRING holding a whole JSON object/array (e.g. ``'{"count": 7}'``) -- almost
    always a WRONG extraction: the query read ``.attr("text")`` on a JSON container (a resolve to a
    JSON detail page, an inlined JSON island) instead of drilling into its key. A GENUINE nested
    field is a real ``dict`` built by ``.extract(...).project()`` -- never a JSON string -- so this
    only ever flags a container grabbed by mistake, not a legitimate structured field."""
    if not isinstance(v, str):
        return False
    s = v.strip()
    if not ((s.startswith("{") and s.endswith("}")) or (s.startswith("[") and s.endswith("]"))):
        return False
    try:
        return isinstance(json.loads(s), (dict, list))
    except (json.JSONDecodeError, ValueError):
        return False

def _is_container_leaf(v: Any) -> bool:
    """A LEAF value that is actually a CONTAINER -- a whole object grabbed instead of a scalar. A
    non-empty ``dict`` (``.resolve().attr("stock")`` returned the ``{"count": 7}`` object), a list that
    holds dicts/lists, or a stringified JSON object/array (``.attr("text")`` on JSON). A list of
    SCALARS is NOT a container -- it is a valid multi-value field (a record's tags), so it is spared."""
    if isinstance(v, dict):
        return len(v) > 0
    if isinstance(v, list):
        return any(isinstance(x, (dict, list)) for x in v)
    return _looks_like_json_blob(v)

def _blob_valued_fields(rows: "list[Any]", brief: Brief) -> "list[str]":
    """Required LEAF fields whose value is a CONTAINER (a whole object/array) on some row instead of a
    scalar -- typically ``.resolve()`` to a JSON detail page then reading the object (``.attr("text")``
    -> a JSON string, or ``.attr("stock")`` -> the ``dict``) rather than drilling to the key. The value
    is non-empty, so the empty-field check misses it, yet it is not real data: the query must select the
    specific key (``.select("stock.count").attr("text")`` / ``.select("stock").attr("count")``). A leaf
    is judged by the BRIEF's schema -- a field declared with sub-fields is a branch (its leaves are the
    scalars) and never flagged; only a field the brief asked for as ONE value that came back a container is."""
    req = _required_leaf_paths(brief)
    dict_rows = [r for r in rows if isinstance(r, dict)]
    if not req or not dict_rows:
        return []
    return [p for p in req if any(_is_container_leaf(_dig(r, p)) for r in dict_rows)]

def _first_row_is_header(good: "list[Any]", brief: Brief) -> bool:
    """The FIRST extracted record looks like the table's HEADER echoed as DATA -- its cell values equal
    the column/field NAMES (``{name:'Name', price:'Price'}``). Happens when ``select_all('tbody tr')``
    catches a header row built from ``<td>`` (not ``<th>``): the phantom row is non-empty, so the
    empty-field check misses it and it ships as a bogus record. Conservative (values must literally
    equal the field keys, >= 2 of them), so real data never trips it."""
    if not good or not isinstance(good[0], dict):
        return False
    r = good[0]
    hits = sum(1 for k, v in r.items()
               if isinstance(v, str) and v.strip() and v.strip().lower() == str(k).strip().lower())
    return hits >= 2 and hits >= max(1, len(r) // 2)

def _longest_record_list(obj: Any, path: str = "") -> "tuple[int, str]":
    """The longest LIST-OF-OBJECTS inside a parsed JSON value, as ``(length, dotted_path)`` -- the
    records of a JSON island. Descends dict keys only (so the path is a clean ``a.b.items`` the DSL's
    ``select_all`` takes); a list at least half objects counts as records."""
    best = (0, "")
    if isinstance(obj, list):
        if obj and sum(isinstance(x, dict) for x in obj) >= len(obj) / 2:
            best = (len(obj), path)
    elif isinstance(obj, dict):
        for k, v in obj.items():
            cand = _longest_record_list(v, f"{path}.{k}" if path else k)
            if cand[0] > best[0]:
                best = cand
    return best

def _uses_json_island(expr: Any) -> bool:
    """Whether the query already reads a JSON island (has an ``as_json`` step) -- if so, it is not a
    DOM-only query and the subset check below does not apply."""
    return any(s.kind == "get" and s.name == "as_json" for s in expr._plan.steps)

def _json_island(doc: Any) -> "tuple[int, str, str] | None":
    """The richest JSON ISLAND in the page as ``(record_count, array_path, script_selector)`` -- the
    longest list-of-objects inside a ``<script type="application/ld+json">`` or ``__NEXT_DATA__`` blob,
    with the EXACT script selector that holds it (so a hint can hand the model a working selector).
    ``None`` if the page has no such island."""
    if not getattr(doc, "ok", False) or getattr(doc, "kind", "html") not in ("html", "xml"):
        return None
    best: "tuple[int, str, str]" = (0, "", "")
    for sel in ('script[type="application/ld+json"]', "script#__NEXT_DATA__"):
        try:
            for el in doc.select_all(sel):
                text = el.attr("text", optional=True)
                if not text:
                    continue
                try:
                    obj = json.loads(text)
                except (json.JSONDecodeError, ValueError):
                    continue
                count, path = _longest_record_list(obj)
                if count > best[0]:
                    best = (count, path, sel)
        except Exception:  # noqa: BLE001 - island detection must never break authoring
            continue
    return best if best[0] > 0 else None

def _richer_json_island(expr: Any, doc: Any, dom_count: int) -> "tuple[int, str, str] | None":
    """When a JSON ISLAND holds the WHOLE dataset the query missed -- returns ``(count, path,
    script_selector)`` for the retry hint, else ``None``. Fires in two cases: (a) the query read the
    VISIBLE DOM (no ``.as_json()``) and the island holds MATERIALLY MORE than ``dom_count`` -- the DOM
    is a teaser subset (Next.js / schema.org); or (b) the query DID try the island (``.as_json()``) but
    extracted 0 rows -- usually a wrong script selector (``application/json`` for ``application/ld+json``)
    -- so the hint hands it the EXACT selector + path that work."""
    island = _json_island(doc)
    if island is None:
        return None
    count, _path, _sel = island
    if _uses_json_island(expr):
        return island if dom_count == 0 else None          # botched island query -> correct the selector
    return island if count > max(dom_count * 2, dom_count + 3) else None  # DOM teaser subset

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
    good_rows = _populated_rows(rows)
    island = _richer_json_island(expr, doc, len(good_rows))  # the visible DOM is a SUBSET of a JSON island
    if island is not None:
        count, path, sel = island
        return (
            f"Your query got {len(good_rows)} record(s), but a JSON ISLAND in this page holds {count} -- the "
            "visible cards are only a TEASER; the whole dataset is in the island. Extract from the island "
            f"with EXACTLY this script selector (copy it verbatim -- it is application/ld+json, NOT "
            f"application/json): wq.doc.select('{sel}').as_json().select_all('{path}')"
            ".extract(<field>=wq.doc.attr('<key>'), ...).project()  -- JSON uses dotted paths + .attr(key), "
            "never .attr('text'); a nested object is .select('<obj>').attr('<key>')."
        )
    if _first_row_is_header(good_rows, brief):  # a header row extracted as a phantom data record
        return (
            f"Your FIRST record is the table's HEADER row echoed as data (its values are the column "
            f"names, e.g. {good_rows[0]}) -- your record selector matched a header <tr> built from <td>. "
            "Exclude it: use .table() (it reads the header row as the keys, not a record), or select only "
            "DATA rows (a header-aware selector like 'tbody tr:not(:first-child)', or the rows' own class)."
        )
    if not good_rows:  # matched a container but every field is empty (or 0 rows)
        return _no_rows_hint(expr, doc) + shown + caveat
    blobs = _blob_valued_fields(good_rows, brief)  # a field grabbed a whole object, not a leaf
    if blobs:
        cols = ", ".join(f'"{c}"' for c in blobs)
        return (
            f"The field(s) {cols} came back as a WHOLE OBJECT (e.g. {{\"count\": 7}}) instead of a single "
            "value. This happens when you .resolve() to a JSON detail page (or an inlined JSON island) "
            "and read the CONTAINER -- .attr(\"text\") on the object gives its JSON string, and "
            ".attr(\"stock\") gives the whole {\"count\": ...} object. JSON is not HTML: DRILL INTO the "
            'key -- use a dotted path .select("stock.count").attr("text"), or select the object and read '
            'its key with .select("stock").attr("count"). Return the leaf VALUE, not the object.'
        )
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
    island = _richer_json_island(expr, doc, len(good))
    if island is not None:
        return f"{len(good)} DOM record(s) but a JSON island holds {island[0]} -- extract from the island"
    if _first_row_is_header(good, brief):
        return "the first record is the table's HEADER row (values = column names) -- exclude it / use .table()"
    if not good:
        n = _selector_match_count(_row_selector(expr), doc)
        if n and _required_field_raises(expr, doc) is not None:  # a required field misses on some rows -> raises
            return f"{n} records matched but a required field is absent on some rows (make it optional)"
        return f"0 rows (record selector matched {n if n is not None else '?'})"
    empty = _empty_required_fields(good, brief)
    if empty:
        return f"required field(s) {', '.join(empty)} empty on every row"
    blobs = _blob_valued_fields(good, brief)
    if blobs:
        return f"field(s) {', '.join(blobs)} grabbed a whole JSON object, not a value (drill into the key)"
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
