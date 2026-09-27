"""Collection and Field: the two surface types that carry a little behaviour.

Every other surface is a method-free wrapper over a core (``webclient.surface``),
but a ``Collection`` (a fanned-out set of results) and a ``Field`` (a scalar
leaf) are the sanctioned exceptions -- they hold minimal built-in helpers.
``Collection`` runs the row-shaping ops (``extract`` / ``filter`` / ``project``
/ ``documents``) by evaluating sub-expressions against each element; ``Field``
is the value leaf (``get`` + ``is_ok``/``is_empty`` + comparisons + truthiness).
"""

from __future__ import annotations

import datetime as _dt
import re

from typing import TYPE_CHECKING, Any, Generic, Iterable, Iterator, Literal, TypeVar, cast, overload  # noqa: F401  (Literal used by generated stubs)

# covariant: Field/Collection/Lazy only ever *produce* T (iterate/index/get/collect),
# never consume it, so ``Collection[AsyncDocument]`` is a ``Collection[Document]``
# -- which lets the async surface override an inherited ``-> Collection[Document]`` op.
T = TypeVar("T", covariant=True)
M = TypeVar("M")  # a row model (e.g. a pydantic BaseModel) for project(model)

if TYPE_CHECKING:
    from ..core.client import WebClient
    from ..core.client.loop import EngineLoop
    from ..interface import Document, Reference


_MONTHS = {m: i + 1 for i, m in enumerate("jan feb mar apr may jun jul aug sep oct nov dec".split())}
_UNITS = {"second": 1, "sec": 1, "minute": 60, "min": 60, "hour": 3600, "hr": 3600, "day": 86400,
          "week": 604800, "month": 2629800, "year": 31557600}


def parse_when(value: Any, *, format: str | None = None, dayfirst: bool = False, now: "_dt.datetime | None" = None) -> "_dt.datetime | None":
    """A datetime from a read (standard library only, so it parses the same everywhere): a
    datetime / date; an explicit ``format``; ISO 8601; ``18 Sep 2026`` / ``Sep 18, 2026`` (with an
    optional time); ``2026/09/18``, ``09/18/2026`` or (``dayfirst``) ``18/09/2026``; ``today`` /
    ``yesterday`` / ``tomorrow`` / ``N units ago``. None when nothing reads as a date."""
    if value is None:
        return None
    if isinstance(value, _dt.datetime):
        return value
    if isinstance(value, _dt.date):
        return _dt.datetime(value.year, value.month, value.day)
    text = " ".join(str(value).split())
    if not text:
        return None
    if format:
        try:
            return _dt.datetime.strptime(text, format)
        except ValueError:
            return None
    iso = re.search(r"\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?)?", text)
    if iso:
        try:
            return _dt.datetime.fromisoformat(iso.group(0).replace("Z", "+00:00"))
        except ValueError:
            pass
    clock = re.search(r"(\d{1,2}):(\d{2})(?::(\d{2}))?\s*([ap]\.?m\.?)?", text, re.I)

    def at(y: int, m: int, d: int) -> "_dt.datetime | None":
        h = mi = sec = 0
        if clock:
            h, mi, sec = int(clock.group(1)), int(clock.group(2)), int(clock.group(3) or 0)
            ampm = (clock.group(4) or "").lower().replace(".", "")
            if ampm == "pm" and h < 12:
                h += 12
            if ampm == "am" and h == 12:
                h = 0
        try:
            return _dt.datetime(y, m, d, h, mi, sec)
        except ValueError:
            return None

    mon = r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?"
    m = re.search(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+" + mon + r",?\s+(\d{4})", text, re.I)
    if m:
        return at(int(m.group(3)), _MONTHS[m.group(2).lower()[:3]], int(m.group(1)))
    m = re.search(mon + r"\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})", text, re.I)
    if m:
        return at(int(m.group(3)), _MONTHS[m.group(1).lower()[:3]], int(m.group(2)))
    m = re.search(r"\b(\d{4})[/.](\d{1,2})[/.](\d{1,2})\b", text)
    if m:
        return at(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    m = re.search(r"\b(\d{1,2})[/.-](\d{1,2})[/.-](\d{2,4})\b", text)
    if m:
        a, b, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        y = y + 2000 if y < 100 else y
        d, mo = (a, b) if (dayfirst or a > 12) else (b, a)
        return at(y, mo, d)
    base = now or _dt.datetime.now()
    low = text.lower()
    for word, days in (("today", 0), ("yesterday", -1), ("tomorrow", 1)):
        if re.search(rf"\b{word}\b", low):
            day = (base + _dt.timedelta(days=days)).date()
            return at(day.year, day.month, day.day)
    m = re.search(r"\b(\d+|an?|one)\s+(second|sec|minute|min|hour|hr|day|week|month|year)s?\s+ago\b", low)
    if m:
        n = 1 if m.group(1) in ("a", "an", "one") else int(m.group(1))
        return (base - _dt.timedelta(seconds=n * _UNITS[m.group(2)])).replace(microsecond=0)
    # the fallback: dateutil for other written forms ("Friday 2026-Sep-18", "18.IX.2026" aside) --
    # strict first; fuzzy only when a month is named, so a stray number never becomes a date
    from dateutil import parser as _du

    try:
        return _du.parse(text, dayfirst=dayfirst)
    except (ValueError, OverflowError):
        pass
    if re.search(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\b", low):
        try:
            return _du.parse(text, dayfirst=dayfirst, fuzzy=True)
        except (ValueError, OverflowError):
            pass
    return None


#: number words `Field.number` reads when a value has no digits (star ratings coded as words).
_NUMBER_WORDS = {w: i for i, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen "
    "sixteen seventeen eighteen nineteen twenty".split())}


class Field(Generic[T]):
    """A scalar leaf: a value plus whether it is present/ok."""

    __slots__ = ("_value", "_ok", "_base", "_client")

    def __init__(self, value: Any = None, *, ok: bool = True) -> None:
        self._value = value
        self._ok = ok and value is not None
        #: where the value was read (the page's URL) and the client that read it -- so ``.link()`` resolves
        #: relative text and the Reference it makes can be resolved
        self._base: "str | None" = None
        self._client: Any = None

    def get(self, default: Any = None) -> T:
        """The field's value, or ``default`` when it is empty/missing."""
        return cast(T, self._value if self._ok else default)

    @property
    def value(self) -> T:
        """The raw stored value (without the empty/missing fallback ``get`` applies)."""
        return cast(T, self._value)

    @property
    def ok(self) -> bool:
        """Whether the field is present (matched and non-None)."""
        return self._ok

    def is_ok(self) -> "Field[bool]":
        """A boolean ``Field`` of whether this field is present -- for use inside a lazy filter."""
        return Field(self._ok)

    def is_empty(self) -> "Field[bool]":
        """A boolean ``Field`` of whether this field is empty by PRESENCE (missing / ``""``/``[]``/
        ``{}``/``None``) -- a matched ``0``/``False`` is NOT empty, unlike ``bool(field)``."""
        # PRESENCE, not truthiness: a matched ``0`` / ``0.0`` / ``False`` field is NOT empty
        # (only "" / [] / {} / None / a miss are). Use this (and ``is_ok``) in ``filter`` --
        # they disagree with ``bool(field)`` on falsy-but-present values like a ``0`` price.
        return Field(not self._ok or self._value in ("", [], {}, None))

    def number(self, default: Any = None) -> "Field[Any]":
        """The value as a NUMBER: the first number in the text (``"£51.77"`` → 51.77, ``"In stock
        (22 available)"`` → 22, ``"1,234"`` → 1234), else a number WORD (``"Three"`` → 3, as
        books.toscrape.com codes its star ratings in a class); ``default`` when there is none."""
        v = self.get()
        if isinstance(v, bool):
            return Field(int(v))
        if isinstance(v, (int, float)):
            return Field(v)
        text = str(v or "")
        m = re.search(r"-?\d[\d,]*(?:\.\d+)?|-?\.\d+", text)
        if m:
            raw = m.group(0).replace(",", "")
            num = float(raw)
            return Field(int(num) if num.is_integer() and "." not in raw else num)
        for word in re.findall(r"[A-Za-z]+", text):
            if word.lower() in _NUMBER_WORDS:
                return Field(_NUMBER_WORDS[word.lower()])
        return Field(default)

    def date(self, format: str | None = None, *, dayfirst: bool = False, default: Any = None) -> "Field[Any]":
        """The value as a DATE, ``YYYY-MM-DD``: ISO text, written dates (``18 Sep 2026``,
        ``September 18, 2026``), numeric ones (``09/18/2026``; ``dayfirst=True`` for
        ``18/09/2026``), relative ones (``today``, ``yesterday``, ``3 days ago``), or an explicit
        ``format`` (``strptime``); ``default`` when there is none."""
        when = parse_when(self.get(), format=format, dayfirst=dayfirst)
        return Field(when.date().isoformat() if when else default)

    def datetime(self, format: str | None = None, *, dayfirst: bool = False, default: Any = None) -> "Field[Any]":
        """The value as a DATETIME, ISO ``YYYY-MM-DDTHH:MM:SS`` (a date alone is midnight; an
        offset is kept) -- the same inputs as :meth:`date`."""
        when = parse_when(self.get(), format=format, dayfirst=dayfirst)
        return Field(when.isoformat(timespec="seconds") if when else default)

    def link(self, base: str | None = None) -> Any:
        """The value as a LINK: a resolvable ``Reference`` (like ``attr("href")``) -- for a URL written as TEXT
        (a ``data-url``, a link in a table cell, ``"/catalogue/x.html"``). Relative text resolves against
        ``base``, else the page it was read on. An empty value is an empty (not-ok) Reference."""
        from urllib.parse import urljoin

        from ..core.reference import from_url

        text = str(self.get() or "").strip()
        ref = from_url(urljoin(base or self._base or "", text) if text else "")
        client = self._client
        if client is not None:
            ref._client = client
        return ref

    def map(self, mapping: dict[str, Any], default: Any = None) -> "Field[Any]":
        """The value looked up in ``mapping`` (strings compare case-insensitively): a code to its
        meaning, a word to a number; ``default`` when it is not there."""
        v: Any = self.get()
        key = v if isinstance(v, str) else str(v)
        if key in mapping:
            return Field(mapping[key])
        if isinstance(v, str):
            low = {str(k).lower(): val for k, val in mapping.items()}
            if v.strip().lower() in low:
                return Field(low[v.strip().lower()])
        return Field(default)

    def split(self, sep: str | None = None, maxsplit: int = -1, *, regex: bool = False,
              strip: bool = True, keep_empty: bool = False) -> "Collection[Field[str]]":
        """The text SPLIT into a Collection of Fields (a list in a row): on ``sep`` (whitespace when
        omitted; a regular expression with ``regex=True``), at most ``maxsplit`` times. Parts are
        stripped and empty ones dropped unless ``strip=False`` / ``keep_empty=True``. A missing
        value splits to an empty Collection. ``"a, b, c".split(",")`` → ``["a", "b", "c"]``."""
        if not self._ok:
            return Collection([])
        text = self._value if isinstance(self._value, str) else str(self._value)
        if regex and sep is not None:
            parts = re.split(sep, text, maxsplit=max(maxsplit, 0))
        else:
            parts = text.split(sep, maxsplit)
        if strip:
            parts = [p.strip() for p in parts]
        if not keep_empty:
            parts = [p for p in parts if p != ""]
        return Collection([Field(p) for p in parts])

    def __bool__(self) -> bool:
        """Value TRUTHINESS (so ``0``/``False`` read as falsy) -- deliberately unlike
        ``is_empty`` (presence); lazy filters should prefer ``is_ok``/``is_empty``."""
        return bool(self._value) if self._ok else False

    def __eq__(self, o: Any) -> bool:  # type: ignore[override]
        """Compare by underlying value (unwrapping the other operand if it is a ``Field`` too)."""
        return bool(self.get() == (o.get() if isinstance(o, Field) else o))

    def __ne__(self, o: Any) -> bool:  # type: ignore[override]
        """The negation of :meth:`__eq__`."""
        return not self.__eq__(o)

    def __hash__(self) -> int:
        """Hash by value (empty fields hash to 0), so a field is usable as a dict/set key."""
        return hash(self._value) if self._ok else 0

    def __repr__(self) -> str:
        """A ``Field(value)`` rendering, or ``Field(<empty>)`` when empty/missing."""
        return f"Field({self._value!r})" if self._ok else "Field(<empty>)"


def _raw(value: Any) -> Any:
    """Unwrap a Field to its raw value (missing -> None); pass anything else
    (a core column -- e.g. a ``Reference`` from ``attr("href")`` -- is kept so a
    later step can follow it; over the wire the service serialises it to a handle)."""
    return value.get() if isinstance(value, Field) else value


def _project_value(value: Any) -> Any:
    """Clean one projected row value to plain data: a ``Reference`` -> its URL
    string, a ``Field`` -> its value, a list -> its cleaned items (a
    ``Document``/element is left as-is -- an un-extracted element is not row data)."""
    from ..core.reference import Reference

    if isinstance(value, Field):
        return _project_value(value.get())
    if isinstance(value, Reference):
        return value.dispatch("url")  # the URL string (pure derive, no IO)
    if isinstance(value, (list, Collection)):  # a LIST-valued field (e.g. select_all(...).attr(...))
        return [_project_value(v) for v in value]
    return value


def _project_row(row: dict[str, Any]) -> dict[str, Any]:
    """A copy of ``row`` with each value cleaned to plain data (see
    :func:`_project_value`). A copy, so the element's stored ``_row`` (which a
    later ``reference(col).resolve()`` may still read) is untouched."""
    return {k: _project_value(v) for k, v in row.items()}


def _distinct_rows(rows: "list[Any]") -> "list[Any]":
    """``rows`` with DUPLICATES dropped, keeping the FIRST occurrence and preserving order. Two rows
    are the same when their data is equal -- compared by a stable JSON key so nested dicts / lists
    compare structurally. This is what makes a paginated query robust to OVERLAPPING pages and a
    sticky/sponsored record repeated on every page: each distinct record appears exactly once."""
    import json

    seen: set[str] = set()
    out: list[Any] = []
    for r in rows:
        try:
            key = json.dumps(r, sort_keys=True, default=str)
        except (TypeError, ValueError):
            key = repr(r)
        if key not in seen:
            seen.add(key)
            out.append(r)
    return out


def flatten_row(row: dict[str, Any], flatten: "bool | Iterable[str] | None", sep: str = ".") -> dict[str, Any]:
    """Merge NESTED dict columns into the row: ``flatten=True`` flattens every nested dict
    (recursively), a list of names only those columns (a dotted name reaches deeper:
    ``"detail.info"``); a flattened column's keys become ``column{sep}key``. Lists are left as
    they are (a list of rows is still a list)."""
    if not flatten:
        return row
    names = None if flatten is True else {str(n) for n in flatten}  # type: ignore[union-attr]

    def go(r: dict[str, Any], prefix: str) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for k, v in r.items():
            path = f"{prefix}{sep}{k}" if prefix else str(k)
            if isinstance(v, dict) and (names is None or path in names or any(n.startswith(path + sep) for n in names)):
                if names is None or path in names:
                    out.update(go(v, path))
                else:  # a deeper name is flattened inside this column; the column itself stays a dict
                    out[k] = go_inner(v, path)
            else:
                out[path if prefix else k] = v
        return out

    def go_inner(r: dict[str, Any], prefix: str) -> dict[str, Any]:
        flat = go(r, prefix)
        cut = len(prefix) + len(sep)
        return {k[cut:] if k.startswith(prefix + sep) else k: v for k, v in flat.items()}

    return go(row, "")


def _row_of(element: Any, *, create: bool = True) -> dict[str, Any] | None:
    """The extracted-columns dict on an element's core (a plain dict element is
    its own row). ``create`` seeds an empty row on first access."""
    if isinstance(element, dict):
        return element
    from ..core.web_core import WebCore

    # a surface IS its core now (_row is a PrivateAttr on the concrete cores).
    core: Any = (
        element if isinstance(element, WebCore) else getattr(element, "_core", None)
    )
    if core is None:
        return None
    if core._row is None and create:
        core._row = {}
    return cast("dict[str, Any] | None", core._row)


def _alias_at(expr: Any) -> "tuple[Any, Any] | None":
    """Split a chain at its trailing ``.alias(name)``: (the value expr, the name -- a literal or
    an Expr), or None when the chain has no alias."""
    from .expr import Expr

    plan = getattr(expr, "_plan", None)
    steps = list(getattr(plan, "steps", []) or []) if plan is not None else []
    for i in range(len(steps) - 1, 0, -1):
        if steps[i].kind == "call" and steps[i - 1].kind == "get" and steps[i - 1].name == "alias":
            arg = steps[i].args[0] if steps[i].args else None
            name = Expr(arg.plan, expr._client) if arg is not None and arg.plan is not None else (arg.value if arg is not None else None)
            assert plan is not None
            return Expr(plan.model_copy(update={"steps": steps[: i - 1]}), expr._client), name
    return None


def split_alias(expr: Any) -> "tuple[Any, Any]":
    """An extract column given POSITIONALLY must end in ``.alias(name)``: split it into
    (the value expression, the name -- a literal or an Expr evaluated per element)."""
    got = _alias_at(expr)
    if got is None:
        from ..errors import WebException, make

        raise WebException(make("plan.invalid", "a positional extract column needs .alias(name): extract(expr.alias('key')) or extract(expr.alias(<expr>))"))
    return got


def field_ref(name: Any) -> "str | None":
    """The column a name expression refers to when it is exactly ``field("x")`` (a previously
    extracted column), else None."""
    steps = list(getattr(getattr(name, "_plan", None), "steps", []) or [])
    if len(steps) == 2 and steps[0].kind == "get" and steps[0].name == "field" and steps[1].kind == "call" and steps[1].args:
        v = steps[1].args[0].value
        return v if isinstance(v, str) else None
    return None


def columns_of(args: "Iterable[Any]", named: dict[str, Any]) -> "list[tuple[Any, Any]]":
    """The (name, value expr) columns of an extract. Plain named columns come first, in order;
    then the ALIASED ones -- positional (``expr.alias(name)``) or named (``value=expr.alias(name)``,
    the alias wins over the keyword) -- so an alias can name a column from the page
    (``.alias(doc.select("th").attr("text"))``) or from a column already extracted
    (``.alias(doc.field("name"))``: that column is then CONSUMED -- used as the name, dropped from the
    row -- so ``select_all("tr").extract(name=th, value=td.alias(field("name"))).merge()`` is a dict)."""
    plain: list[tuple[Any, Any]] = []
    aliased: list[tuple[Any, Any]] = []
    plain_segs: list[tuple[str, str]] = []
    aliased_segs: list[tuple[str, str]] = []
    for key, expr in named.items():
        got = _alias_at(expr)
        if got is None:
            plain.append((key, expr))
            plain_segs.append((f"kw:{key}", ""))
        else:
            aliased.append((got[1], got[0]))
            aliased_segs.append((f"kw:{key}", f"kw:{key}/{_steps_len(got[0])}/arg:0"))
    for n, expr in enumerate(args):
        value, name = split_alias(expr)
        aliased.append((name, value))
        aliased_segs.append((f"arg:{n}", f"arg:{n}/{_steps_len(value)}/arg:0"))
    out = _Columns(plain + aliased)
    out.segs = plain_segs + aliased_segs
    return out


class _Columns(list):  # type: ignore[type-arg]
    """An extract's columns, with where each sits in the extract call (its value's arg segment and its
    aliased name's) -- so the steps a column runs are addressed in the plan (``events.CURRENT_STEP``)."""

    segs: "list[tuple[str, str]]"


def _steps_len(expr: Any) -> int:
    return len(getattr(getattr(expr, "_plan", None), "steps", []) or [])


async def apply_extract(element: Any, columns: "dict[str, Any] | list[tuple[Any, Any]]", client: "WebClient | None") -> None:
    """Annotate ``element``'s row with the evaluated columns (unwrapped, stored in
    order so a later column can reference an earlier one). Loud by default: a column
    whose ``select``/``attr`` misses raises (naming the selector) -- mark a genuinely
    optional field with ``error=RETURN`` (or ``optional=True``) on its select to get
    ``None`` instead. An aliased column's name is evaluated per element; a name that is a
    previous column (``field("x")``) consumes that column. THE one row-extraction
    implementation -- shared by the eager (:meth:`Collection.aextract`) and streaming
    (``executor._astream_collection``) paths so they cannot diverge."""
    from .executor import aevaluate

    row = _row_of(element)
    if row is None:
        return
    from .executor import arg_segment

    pairs = list(columns.items()) if isinstance(columns, dict) else columns
    segs = getattr(columns, "segs", None) or [(f"kw:{k}" if isinstance(k, str) else "", "") for k, _ in pairs]
    consumed: list[str] = []
    for (key, expr), (seg, name_seg) in zip(pairs, segs):
        if not isinstance(key, str):  # an aliased column: the name is read off the element / an earlier column
            ref = field_ref(key)
            if ref is not None and ref in row:
                key = str(_raw(row[ref]) or "").strip() or ref
                consumed.append(ref)
            else:
                with arg_segment(name_seg):
                    key = str(_raw(await aevaluate(key, element, client=client)) or "").strip() or "field"
        with arg_segment(seg):
            row[key] = _raw(await aevaluate(expr, element, client=client))
    for ref in consumed:  # a column used as a name is spent
        row.pop(ref, None)


async def survives_filters(element: Any, predicates: "Iterable[Any]", client: "WebClient | None") -> bool:
    """Whether ``element`` passes every predicate. Loud by default (a predicate that
    references a missing field raises) -- mark an optional select ``error=RETURN`` /
    ``optional=True`` to treat a miss as a non-match. The one filter implementation,
    shared by eager and streaming paths."""
    from .executor import aevaluate, truthy

    from .executor import arg_segment

    for n, pred in enumerate(predicates):
        with arg_segment(f"arg:{n}"):
            if not truthy(await aevaluate(pred, element, client=client)):
                return False
    return True


class Collection(Generic[T]):
    """A set of results (elements or rows). Iterable/indexable; the row-shaping
    ops evaluate sub-expressions per element, and element ops fan out."""

    __slots__ = ("_items", "_client", "name", "root", "_kept", "_stop")

    def __init__(
        self, items: list[Any] | None = None, *, client: "WebClient | None" = None, root: str = ""
    ) -> None:
        self._items = items or []
        self._client = client
        #: a filtered collection: the POSITIONS its items had in the collection they were filtered from (an item
        #: numbered after a filter is its position here -- this maps it back to the element it is)
        self._kept: "list[int] | None" = None
        #: a pager's pages: why its walk stopped and how many pages it fetched (``{"stop", "fetched"}``)
        self._stop: "dict[str, Any] | None" = None
        self.root = root
        self.name = f"col:{root}" if root else "col:"

    # -- container ------------------------------------------------------------
    def __iter__(self) -> Iterator[T]:
        """Iterate the collection's items."""
        return iter(self._items)

    def __len__(self) -> int:
        """The number of items in the collection."""
        return len(self._items)

    def __getitem__(self, i: int) -> T:
        """The item at index ``i``."""
        return cast(T, self._items[i])

    def __repr__(self) -> str:
        """A compact ``Collection(N items)`` rendering."""
        return f"Collection({len(self._items)} items)"

    if TYPE_CHECKING:
        # >>> generated: collection element-op lifting <<<
        # fmt: off
        def as_json(self) -> "Collection[Document]": ...
        def attr(self, name: str, pattern: str | None = ..., *, group: int | str | None = ..., optional: bool = ..., error: Any = ...) -> "list[str | None]": ...
        def back(self, *, timeout: float | None = ...) -> "Collection[Document]": ...
        def click(self, selector: str | None = ..., *, timeout: float | None = ..., optional: bool = ..., error: Any = ...) -> "Collection[Document]": ...
        def element_table(self, *, interactive: bool = ...) -> "list[str]": ...
        def framework(self) -> "list[str | None]": ...
        def goto(self, url: str, *, timeout: float | None = ...) -> "Collection[Document]": ...
        def html(self) -> "list[str]": ...
        def is_empty(self) -> "list[bool | None]": ...
        def is_ok(self) -> "list[bool | None]": ...
        def links(self) -> "Collection[Reference]": ...
        def markdown(self, *, main_content_only: bool = ...) -> "list[str]": ...
        def message(self) -> "list[str]": ...
        def next_link(self) -> "Collection[Reference]": ...
        def ref(self) -> "Collection[Reference]": ...
        def regex(self, pattern: str, *, group: int | str = ..., flags: str = ...) -> "list[str | None]": ...
        def region(self) -> "list[str]": ...
        def reload(self) -> "Collection[Document]": ...
        def render(self, format: str, **options: Any) -> "list[str]": ...
        def screenshot(self, selector: str | None = ...) -> "Collection[Document]": ...
        def scroll(self, selector: str | None = ..., *, timeout: float | None = ...) -> "Collection[Document]": ...
        def select(self, selector: str, *, index: int = ..., optional: bool = ..., error: Any = ...) -> "Collection[Document]": ...
        def select_all(self, selector: str, *, limit: int | None = ..., offset: int = ...) -> "Collection[Document]": ...
        def skeleton(self, *, max_lines: int = ..., text_chars: int = ..., max_depth: int = ..., max_siblings: int = ..., legend: bool = ..., collapse: bool = ..., drop_chrome: bool = ..., annotate_origin: bool = ..., correlate: bool = ..., mark_records: bool = ..., mark_interactive: bool = ...) -> "list[str]": ...
        def table(self, selector: 'str | None' = ..., *, transpose: bool = ...) -> "Collection[Document]": ...
        def text(self, *, main_content_only: bool = ...) -> "list[str]": ...
        def title(self) -> "list[str | None]": ...
        def wait_for(self, selector: str | None = ..., *, timeout: float | None = ..., optional: bool = ..., error: Any = ...) -> "Collection[Document]": ...
        def write(self, selector: str, text: str, *, timeout: float | None = ..., optional: bool = ..., error: Any = ...) -> "Collection[Document]": ...
        # fmt: on
        # >>> end generated <<<
    else:

        def __getattr__(self, name: str) -> Any:
            """An element op fans out over the elements: a list of results (a
            Collection when the results are surfaces)."""
            if name.startswith("_"):
                raise AttributeError(name)

            def fan(*args: Any, **kwargs: Any) -> Any:
                from ..core.web_core import WebCore

                def apply(el: Any) -> Any:
                    attr = getattr(el, name)
                    # a prop op already resolved to its value (not callable); a
                    # call op returns a dispatcher we invoke with the args. Works
                    # for a core surface and a remote handle alike.
                    return attr(*args, **kwargs) if callable(attr) else attr

                results = [apply(el) for el in self._items]
                if results and all(isinstance(r, WebCore) for r in results):
                    return Collection(results, client=self._client, root=self.root)
                # scalars fan out to a plain list of RAW values (the eager tier never
                # exposes a Field -- that is a lazy/recorder wrapper).
                return [_raw(r) for r in results]

            return fan

    # -- row shaping ----------------------------------------------------------
    def _loop(self) -> "EngineLoop":
        """The engine loop the eager row-shaping ops bridge onto (this collection's client,
        else the shared default)."""
        from ..core.client import default_client

        return (self._client or default_client()).loop()

    async def aextract(self, *aliased: Any, **exprs: Any) -> "Collection[T]":
        """Annotate each element with extracted columns (its ``_row``): columns
        are evaluated in order against the element (a later column can reference
        an earlier one via ``field``; chained extracts accumulate); elements are
        evaluated concurrently, bounded by the pool. Fields store unwrapped. A
        positional column ends in ``.alias(name)`` -- ``name`` a literal or an
        expression read off the element, so a key/value table becomes a dict."""
        from .executor import fan_out

        columns = columns_of(aliased, exprs)

        async def one(el: Any) -> None:
            await apply_extract(el, columns, self._client)

        await fan_out(list(self._items), one, limit=self._limit(), bus=getattr(self._client, "bus", None))
        return self._derive(self._items)

    async def afilter(self, *predicates: Any) -> "Collection[T]":
        """Keep the elements for which every predicate is truthy."""
        from .executor import fan_out

        async def keep(el: Any) -> bool:
            return await survives_filters(el, predicates, self._client)

        flags = await fan_out(list(self._items), keep, limit=self._limit(), bus=getattr(self._client, "bus", None))
        kept = [el for el, ok in zip(self._items, flags) if ok]
        out = self._derive(kept)
        out._kept = [i for i, ok in enumerate(flags) if ok]
        return out

    def _limit(self) -> int:
        """The per-collection fan-out concurrency (the client's pool-portion), bounding how
        many elements are extracted/filtered at once."""
        from .executor import _fanout_limit

        return _fanout_limit(self._client)

    def extract(self, *aliased: Any, **exprs: Any) -> "Collection[T]":
        """Eager form of :meth:`aextract` (bridged onto the engine loop)."""
        return self._loop().run(self.aextract(*aliased, **exprs))

    def merge(self) -> dict[str, Any]:
        """Fold the elements' extracted rows into ONE dict (later rows win on a repeated key):
        the shape of a key/value table -- ``select_all("tr").extract(td.alias(th)).merge()``."""
        out: dict[str, Any] = {}
        for el in self._items:
            row = _row_of(el, create=False)
            if row:
                out.update(_project_row(row))
        return out

    def filter(self, *predicates: Any) -> "Collection[T]":
        """Eager form of :meth:`afilter` (bridged onto the engine loop)."""
        return self._loop().run(self.afilter(*predicates))

    def documents(self, column: str) -> "Collection[Any]":
        """Flatten a column whose values are Collections/lists of documents
        into one Collection of those documents."""
        out: list[Any] = []
        for el in self._items:
            value = (_row_of(el) or {}).get(column)
            out.extend(list(value) if value is not None else [])
        return self._derive(out)

    def limit(self, n: int) -> "Collection[T]":
        """Keep at most the first ``n`` elements."""
        return self._derive(self._items[:n])

    @overload
    def project(self, *, flatten: "bool | list[str] | None" = None, sep: str = ".",
                distinct: bool = False) -> list[dict[str, Any]]:
        """Project each element's row to a plain ``dict``."""
        ...
    @overload
    def project(self, model: type[M], *, flatten: "bool | list[str] | None" = None, sep: str = ".",
                distinct: bool = False) -> list[M]:
        """Project each element's row validated into ``model``."""
        ...

    def project(self, model: type[M] | None = None, *, flatten: "bool | list[str] | None" = None,
                sep: str = ".", distinct: bool = False) -> list[Any]:
        """Materialise as a plain list: each element's extracted row (cleaned to
        plain data -- a ``Reference`` column becomes its URL string, a ``Field`` its
        value), or the element itself if it has no row. ``model`` validates each row
        into it. Eager only -- a model class isn't part of the serialisable plan, so
        call this on a materialised Collection (``...extract(...).collect().project(Model)``).
        ``flatten`` merges nested dict columns into each row (see :func:`flatten_row`).
        ``distinct`` drops DUPLICATE rows (keeping the first, order preserved) -- so a
        paginated walk whose pages OVERLAP, or that repeats a sticky/sponsored record on
        every page, yields each record once instead of many times."""
        out: list[Any] = []
        for el in self._items:
            row = _row_of(el, create=False)
            out.append(flatten_row(_project_row(row), flatten, sep) if row is not None else el)
        if distinct:
            out = _distinct_rows(out)
        if model is None:
            return out
        validate = getattr(model, "model_validate", None)
        return [validate(r) if validate is not None else model(**r) for r in out]

    def _derive(self, items: list[Any]) -> "Collection[T]":
        """A new collection of ``items`` inheriting this one's client / root / name -- the shared
        way the shaping ops return a transformed collection."""
        out: Collection[T] = Collection(items, client=self._client, root=self.root)
        out.name = self.name
        return out


__all__ = ["Collection", "Field"]
