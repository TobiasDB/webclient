"""The STEP-BY-STEP authoring engine (``engine="steps"`` of the authoring loop).

Instead of asking the model for the WHOLE ``wq`` chain in one reply, the query is built ONE OP AT A
TIME over the conversation. Each turn the model sees the QUERY SO FAR plus the RESULT of its last op
-- how many records matched, the first record's structure, the values every column reads on the
first records -- and calls exactly one op:

  ``records("<css>")``                 the repeating record (``select_all``) -- required first
  ``field(name, <wq.doc chain>)``      a column read inside each record (``wq.doc`` = the record)
  ``detail("<link css>")``             follow each record's link ONCE; the result shows that page
  ``detail_field(name, <chain>)``      a column read on the detail page (``wq.doc`` = that page)
  ``where(<predicate>)``               a ``filter`` on the records
  ``drop(name)``                       remove a column
  ``absent(name)``                     the field is NOT on this page (nor its detail) -- no guessing
  ``identity(<field|css>, ...)``       what makes a RECORD unique (default: every field)
  ``detail_identity(<field|css>, ...)`` the detail page's own identity (a stable element)
  ``section("<css>")``                 the dataset continues in ANOTHER section of the page with a
                                       different record shape (an Upcoming tab vs a Past list):
                                       start a new section; the pipeline concatenates the rows
  ``done()``                           finished

Every op is PROBED against the ONE fetched document before the next turn (a detail fan-out is
bounded to the first :data:`_PROBE_ROWS` records via ``limit``, and the detail pages are fetched
once for the whole session through the DSL's resolve memo); an op whose probe fails (a loud select
miss, an unknown DSL verb, a 0-match record selector, a column that reads EMPTY on every record) is
NOT applied / REVERTED with the reason, so the draft is always a runnable query. An op the model
already tried is refused with its earlier result, so the loop cannot repeat itself. The page is
still sent ONCE (the cache-marked opening); a step's feedback is a few hundred chars. A stateless
model (no :class:`~web.onboard.llm.Conversational`) gets the opening plus a one-line-per-step
history each turn -- the same semantics.

The engine is one :class:`~web.onboard.agent.BoundedLoop` (observe = send the turn + parse the op,
decide = the op or done, apply = probe + feedback) run by the outer authoring loop's author turn:
a repair / deepen / sibling note from the outer loop simply becomes the next turn, and the draft
carries over -- the model fixes ONE column instead of re-writing the chain. Every DSL verb the
model reaches for is TALLIED (known and unknown) -- the record of the gaps between what a model
wants to write and what the surface offers.
"""

from __future__ import annotations

import ast
import asyncio
import json
import re
from collections import Counter
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from web.dsl import SourceError, UnknownVerb, from_source, resolve_memo, verbs_of
from web.fetch import WebException, emit
from web.parse import Document, Element
from web.parse.classes import is_noise_class
from web.resolve import Flag, Resolver

from .agent import BoundedLoop, Done
from .compile import Query, clean_reply, parse_query, reroot
from .evaluate import skeleton_for
from .llm import Conversation, Conversational, Llm, ReasonEvent
from .models import DatasetBrief, Reference
from .patterns import steps_prompt
from .prompts import clip

#: the op vocabulary the model may call (see the module docstring).
OPS = frozenset(
    {
        "records",
        "field",
        "detail",
        "detail_field",
        "where",
        "drop",
        "absent",
        "section",
        "identity",
        "detail_identity",
        "done",
    }
)
#: how many records a probe runs the draft over (bounds a detail fan-out to this many fetches).
_PROBE_ROWS = 3
#: a probe's wall clock -- a step must never hang the loop.
_PROBE_TIMEOUT = 30.0
#: how many records() picks that match NOTHING (in a row) prove the records are not in the HTML.
_MAX_ZERO_PICKS = 3
#: how many CONSECUTIVE non-applied ops (rejected / reverted / refused) end a run: a rejection is a
#: legitimate turn the model learns from, so the guard is looser than the base loop's default.
_MAX_STALLS = 5
#: how many records' links are sampled to pick a TYPICAL detail page (not an odd first one).
_LINK_SAMPLE = 12
_PROBE_ROWS_OPTIONAL = 8  # how far an OPTIONAL read is probed before "empty everywhere" counts
_RECORD_CHARS = 2_000  # the FIRST record's structure, shown after records(...)
_DETAIL_CHARS = 6_000  # the detail page's skeleton, shown after detail(...)
_VALUE_CHARS = 80  # one column value in the feedback
#: the column a detail fan-out nests under (``detail={...}``).
DETAIL_COLUMN = "detail"
#: results that say an op was NOT applied because something ELSE must happen first -- the same op
#: is legitimate once that has happened, so these are never memorised as the op's result.
_PRECONDITION = ("no records yet", "no detail link", "current section unfinished")


class StepError(ValueError):
    """The reply was not ONE valid op call. ``verbs`` / ``unknown`` carry the DSL verbs a rejected
    chain reached for anyway, so the verb record counts them."""

    def __init__(
        self, message: str, *, verbs: "Sequence[str]" = (), unknown: "Sequence[str]" = ()
    ) -> None:
        super().__init__(message)
        self.verbs = list(verbs)
        self.unknown = list(unknown)


@dataclass
class Op:
    """One parsed op call: its name, its arguments as SOURCE (a css / a name / a wq chain), and
    the DSL verbs its chain argument calls."""

    name: str
    args: "list[str]" = field(default_factory=list)
    verbs: "list[str]" = field(default_factory=list)

    def line(self) -> str:
        return f"{self.name}(" + ", ".join(self.args) + ")"


@dataclass
class Draft:
    """The query under construction, as its PARTS -- rendered to one ``wq`` chain by
    :meth:`source` (so a probe and the final query are the same text the parser rebuilds)."""

    records: str = ""
    fields: "dict[str, str]" = field(default_factory=dict)  # name -> chain (on the record)
    link: str = ""  # the css of the link followed ONCE per record
    detail_fields: "dict[str, str]" = field(default_factory=dict)  # name -> chain (detail page)
    where: str = ""
    identity: "list[str] | None" = None  # the record's identity parts (None = not declared)
    detail_identity: "list[str] | None" = None  # the detail page's identity parts

    def copy(self) -> "Draft":
        return Draft(
            self.records,
            dict(self.fields),
            self.link,
            dict(self.detail_fields),
            self.where,
            list(self.identity) if self.identity is not None else None,
            list(self.detail_identity) if self.detail_identity is not None else None,
        )

    def names(self) -> "list[str]":
        """Every column the draft reads, detail ones as ``detail.<name>``."""
        return [*self.fields, *(f"{DETAIL_COLUMN}.{n}" for n in self.detail_fields)]

    def columns(self) -> "dict[str, str]":
        cols = dict(self.fields)
        if self.link and self.detail_fields:
            inner = ", ".join(f"{n}={c}" for n, c in self.detail_fields.items())
            cols[DETAIL_COLUMN] = (
                f'wq.doc.select({self.link!r}).attr("href").resolve().extract({inner})'
                + (
                    ".identity(" + ", ".join(repr(p) for p in self.detail_identity) + ")"
                    if self.detail_identity is not None
                    else ""
                )
            )
        return cols

    def source(self, *, limit: int = 0, skip: int = 0) -> str:
        """The draft as a ``wq.doc...`` chain (``""`` before a record selector); ``skip`` /
        ``limit`` bound the records for a probe (the skip comes BEFORE the columns, so a header
        row never reaches a loud select)."""
        if not self.records:
            return ""
        q = f"wq.doc.select_all({self.records!r})"
        if self.where:
            q += f".filter({self.where})"
        if skip:
            q += f".skip({skip})"
        if limit:
            q += f".limit({limit})"
        cols = self.columns()
        if cols:
            q += ".extract(" + ", ".join(f"{n}={c}" for n, c in cols.items()) + ")"
            if self.identity is not None:
                q += ".identity(" + ", ".join(repr(p) for p in self.identity) + ")"
        return q


@dataclass
class StepSession:
    """The engine's memory across the outer loop's turns: the fetched document, the draft, the
    conversation (opening sent once), the one-line-per-step history (a stateless model's
    context), every op tried (so a repeat is refused), the fields declared absent, the verb
    tally, and the text of the next turn."""

    doc: Document
    detail_doc: "Document | None" = None  # the sampled detail page (once detail() is applied)
    draft: Draft = field(default_factory=Draft)  # the CURRENT section
    finished: "list[Draft]" = field(default_factory=list)  # earlier sections of this page
    conv: "Conversation | None" = None
    opened: bool = False
    opening: str = ""
    history: "list[str]" = field(default_factory=list)
    tried: "dict[str, str]" = field(default_factory=dict)  # op line -> its one-line result
    absent: "set[str]" = field(default_factory=set)
    verbs: "Counter[str]" = field(default_factory=Counter)
    unknown: "list[str]" = field(default_factory=list)
    steps: int = 0
    zero_picks: int = 0  # consecutive records() picks that matched NOTHING (a shell page tells)
    skip: int = (
        0  # leading records that are NOT typical (a table header row) -- the probe skips them
    )
    record_selector: str = ""  # the record selector LOCATE detected (a nudge on a different pick)
    pending: str = ""  # the next turn's text (a result / the outer loop's note)
    done: bool = False  # the last run reached done()
    sibling: str = ""
    offer_sibling: bool = False


@dataclass
class StepResult:
    """What one run of the engine hands the outer loop."""

    query: "Query | None" = None
    error: str = ""
    hint: str = ""
    sibling: str = ""
    note: str = ""  # a one-line remark for the rejection trail (a budget stop, ...)
    #: the records are NOT on the fetched page: every record selector the model tried matched
    #: nothing (a shell page) -- a defined stop for the outer loop, not a repair
    no_records: bool = False
    absent: "set[str]" = field(default_factory=set)
    #: the FINISHED earlier sections of this page (query + its rows); ``query`` is the last one.
    sections: "list[tuple[Query, list[object]]]" = field(default_factory=list)


@dataclass
class _Obs:
    op: "Op | None" = None
    error: str = ""
    sibling: str = ""
    stop: bool = False  # the engine itself ends the run (nothing on the page to author)


# -- parsing one op call ---------------------------------------------------------------------------


def _arg_source(node: ast.expr) -> str:
    """One call argument as SOURCE: a string literal's value, a bare name, or the expression text
    (a ``wq.doc`` chain / a predicate)."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        return node.id
    return ast.unparse(node)


_ARITY = {
    "records": 1,
    "field": 2,
    "detail": 1,
    "detail_field": 2,
    "where": 1,
    "drop": 1,
    "absent": 1,
    "section": 1,
    "identity": -1,  # variadic: 0..n parts (field names / css selectors)
    "detail_identity": -1,
    "done": 0,
}


def parse_op(reply: str) -> Op:
    """The ONE op call in a reply -> :class:`Op`. Prose, several calls, an unknown op, a wrong
    arity, a non-identifier column name or an unparseable chain raise :class:`StepError` with the
    reason the model needs to hear."""
    text = clean_reply(reply)
    hits = [i for i in (text.find(f"{name}(") for name in OPS) if i != -1]
    if not hits:
        raise StepError(
            "not an op call -- reply with exactly ONE of records(...) / field(...) / detail(...) / "
            "detail_field(...) / where(...) / drop(...) / absent(...) / section(...) / done()"
        )
    code = _call_span(text[min(hits) :].strip())  # the call alone -- trailing prose dropped
    try:
        tree = ast.parse(code, mode="eval")
    except SyntaxError as exc:
        raise StepError(f"the op call did not parse ({exc.msg}) -- one complete call only") from exc
    node = tree.body
    if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in OPS):
        raise StepError('not an op call -- exactly one op, e.g. records("li.item")')
    args = [_arg_source(a) for a in node.args] + [_arg_source(kw.value) for kw in node.keywords]
    name = node.func.id
    if _ARITY[name] >= 0 and len(args) != _ARITY[name]:
        raise StepError(f"{name}() takes {_ARITY[name]} argument(s), got {len(args)}")
    verbs: list[str] = []
    if name in ("field", "detail_field"):
        col, chain = args
        if not col.isidentifier():
            raise StepError(f"{col!r} is not a valid column name (letters, digits, underscores)")
        verbs = _check_chain(chain, "the column chain")
    elif name == "where":
        verbs = _check_chain(args[0], "the predicate")
    elif name in ("drop", "absent") and not args[0].isidentifier():
        raise StepError(f"{args[0]!r} is not a field name")
    return Op(name, args, verbs)


def _call_span(code: str) -> str:
    """``code`` up to the paren that closes its first call (quotes honoured) -- so prose after the
    call is dropped; unbalanced input is returned whole (and fails the parse loudly)."""
    depth, quote = 0, ""
    for i, ch in enumerate(code):
        if quote:
            if ch == quote:
                quote = ""
        elif ch in "\"'":
            quote = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return code[: i + 1]
    return code


def _check_chain(chain: str, what: str) -> "list[str]":
    """Validate a chain argument through the DSL's own parser -> the verbs it calls. An unknown verb
    is a :class:`StepError` naming the gap (its verbs still ride on the error for the tally)."""
    if not chain.lstrip().startswith(("wq.", "~wq.", "(")):
        raise StepError(f"{what} must be a wq.doc... chain (it began {chain[:40]!r})")
    try:
        return verbs_of(from_source(chain))
    except UnknownVerb as exc:
        raise StepError(
            f"{what} uses {exc} -- use only the verbs in the guide",
            verbs=exc.used,
            unknown=exc.verbs,
        ) from exc
    except SourceError as exc:
        raise StepError(f"{what} is not valid wq: {exc}") from exc


# -- probing the draft ---------------------------------------------------------------------------


async def _count(draft: Draft, doc: Document, resolver: Resolver) -> int:
    """How many records ``draft.records`` matches on the document."""
    expr = parse_query(f"wq.doc.select_all({draft.records!r})")
    got = await asyncio.wait_for(expr.acollect(doc, resolver=resolver), timeout=_PROBE_TIMEOUT)
    if isinstance(got, list):
        return len(got)
    return 0 if got is None else 1


async def _probe(
    draft: Draft, doc: Document, resolver: Resolver, *, skip: int = 0, rows: int = _PROBE_ROWS
) -> "list[object]":
    """The draft run over ``rows`` TYPICAL records of the fetched document -- the first ``skip``
    matches (a table's header row, a featured item) are left out."""
    src = draft.source(limit=rows, skip=skip)
    got = await asyncio.wait_for(
        parse_query(src).acollect(doc, resolver=resolver), timeout=_PROBE_TIMEOUT
    )
    return list(got) if isinstance(got, list) else [got]


def _leaf(el: Element) -> bool:
    """No element children (``*`` matches the element itself too, so one match = a leaf)."""
    return len(el.select_all("*")) <= 1


def _shape(el: Element) -> "tuple[str, ...]":
    """A record's structural signature: the tags of its text-bearing leaves (``th`` vs ``td`` tells
    a header row from a data row; ``h2``/``a``/``time`` tells an item from an ad)."""
    return tuple(sorted(e.tag for e in el.select_all("*") if _leaf(e) and e.text.strip()))


def _typical(doc: Document, css: str) -> "tuple[int, int]":
    """``(index of the first TYPICAL record, how many leading records are not typical)`` among the
    first few matches: the typical shape is the most common one (a table's header row is the odd
    one out; so is a featured first item)."""
    els = doc.select_all(css)[:6]
    if len(els) < 2:
        return 0, 0
    shapes = [_shape(e) for e in els]
    common = max(set(shapes), key=shapes.count)
    first = next(i for i, sh in enumerate(shapes) if sh == common)
    return first, first


def _first_record(doc: Document, css: str) -> str:
    """A TYPICAL matched record's structure (see :func:`_typical`) -- an HTML fragment's skeleton,
    or the first JSON item -- with a note when the first match differs from it."""
    if doc.kind == "json":
        val = doc.at(css)
        first = val[0] if isinstance(val, list) and val else val
        return clip(
            json.dumps(first, ensure_ascii=False, indent=1, default=str), _RECORD_CHARS, "record"
        )
    els = doc.select_all(css)
    if not els:
        return ""
    index, _skip = _typical(doc, css)
    el = els[index]
    frag = Document(content=el.html.encode("utf-8"), kind="html", url=doc.url)
    skel = clip(
        frag.skeleton(max_lines=120, text_chars=60, mark_records=False, mark_interactive=False),
        _RECORD_CHARS,
        "record",
    )
    if index == 0:
        return skel
    odd = Document(content=els[0].html.encode("utf-8"), kind="html", url=doc.url)
    odd_skel = clip(odd.skeleton(max_lines=20, text_chars=30, mark_records=False), 500, "record")
    return (
        f"(record {index + 1} shown -- the typical shape; records 1-{index} differ, e.g. a table "
        f"header or a featured item, and are skipped by the probe:\n{odd_skel}\n-- typical record:)\n"
        + skel
    )


def _record_links(doc: Document, records: str, css: str) -> "list[str]":
    """The hrefs the first records' ``css`` element carries (a JSON record: the value at key
    ``css``), in record order -- up to :data:`_LINK_SAMPLE` of them."""
    out: list[str] = []
    if doc.kind == "json":
        val = doc.at(records)
        items = val if isinstance(val, list) else [val]
        for item in items[:_LINK_SAMPLE]:
            got = item.get(css) if isinstance(item, dict) else None
            if isinstance(got, str) and got.startswith(("http://", "https://")):
                out.append(got)
        return out
    for rec in doc.select_all(records)[:_LINK_SAMPLE]:
        el = rec.select(css)
        href = el.attr("href") if el is not None else None
        if href:
            out.append(href)
    return out


def _typical_link(links: "list[str]") -> "str | None":
    """The link to sample as THE detail page: the first of the LARGEST group sharing a URL shape
    (host + first two path segments) -- so a listing whose first item is an odd one (a live blog
    among articles, a video among posts) still shows the model a representative detail page."""
    if not links:
        return None

    def shape(url: str) -> str:
        parts = urlsplit(url)
        return parts.netloc + "/" + "/".join(parts.path.strip("/").split("/")[:2])

    groups: Counter[str] = Counter(shape(u) for u in links)
    best = groups.most_common(1)[0][0]
    return next(u for u in links if shape(u) == best)


#: the leaf-shaping transforms a chain may end with -- stripped to see what a selector READ.
_TRANSFORMS = ("number", "date", "datetime", "split", "map", "link", "regex")


def _reads(chain: str) -> str:
    """``chain`` cut after its last ``.attr(...)`` / ``.text()`` read -- the selector + read without
    the trailing transforms, so a failed transform can be shown the raw text it was given."""
    last = max(chain.rfind(".attr("), chain.rfind(".text("))
    if last == -1:
        return chain
    depth = 0
    for i in range(last, len(chain)):
        if chain[i] == "(":
            depth += 1
        elif chain[i] == ")":
            depth -= 1
            if depth == 0:
                return chain[: i + 1]
    return chain


async def _raw_values(draft: Draft, op: Op, doc: Document, resolver: Resolver) -> str:
    """For a column that read EMPTY: the RAW values its selector + read produce (transforms
    stripped) on the probed records -- ``""`` when the selector itself matched nothing."""
    raw_chain = _reads(op.args[1])
    if raw_chain == op.args[1] or not any(f".{t}(" in op.args[1] for t in _TRANSFORMS):
        return ""
    probe = draft.copy()
    if op.name == "detail_field":
        probe.detail_fields[op.args[0]] = raw_chain
    else:
        probe.fields[op.args[0]] = raw_chain
    try:
        rows = await _probe(probe, doc, resolver)
    except (WebException, asyncio.TimeoutError):
        return ""
    key = f"{DETAIL_COLUMN}.{op.args[0]}" if op.name == "detail_field" else op.args[0]
    vals = [_dig(r, key) for r in rows]
    return "" if all(_empty(v) for v in vals) else ", ".join(_short(v) for v in vals)


# -- failure hints -------------------------------------------------------------------------------

_SELECT_ARG = re.compile(r"""select(?:_all)?\(\s*(['"])(.*?)\1""")
#: attributes that never identify a value (layout / event noise).
_SKIP_ATTRS = frozenset({"class", "style", "onclick", "tabindex"})


def _tokens(text: str) -> "set[str]":
    return {t.lower() for t in re.findall(r"[A-Za-z][\w-]*", text)}


def _candidates(el: Element) -> "list[str]":
    """The selectors this element answers to: its tag, ``tag.class`` (semantic classes), ``#id``,
    ``tag[attr]`` for its data / semantic attributes."""
    attrs = el.attrs
    tag = el.tag or "*"
    out = [tag]
    out += [f"{tag}.{c}" for c in attrs.get("class", "").split() if not is_noise_class(c)][:3]
    if attrs.get("id"):
        out.append(f"#{attrs['id']}")
    out += [f"{tag}[{a}]" for a in attrs if a not in _SKIP_ATTRS][:4]
    return out


def suggest_selectors(scope: "Document | Element", css: str, *, limit: int = 6) -> "list[str]":
    """The selectors in ``scope`` (the first record, the detail page, the whole page) CLOSEST to a
    ``css`` that missed -- ranked by the tag / class / attribute tokens they share with it. Empty
    when nothing in the scope shares a token (then :func:`leaf_selectors` shows what IS there)."""
    want = _tokens(css)
    scored: dict[str, int] = {}
    for el in scope.select_all("*")[:600]:
        for cand in _candidates(el):
            shared = len(want & _tokens(cand))
            if shared and scored.get(cand, 0) < shared:
                scored[cand] = shared
    # more shared tokens first; on a tie the more SPECIFIC selector (tag.class over tag)
    return [c for c, _ in sorted(scored.items(), key=lambda kv: (-kv[1], -len(kv[0]), kv[0]))][
        :limit
    ]


def leaf_selectors(scope: "Document | Element", *, limit: int = 8) -> "list[str]":
    """The TEXT-bearing elements of ``scope`` as selectors with a text hint -- what a field could
    read when the missed selector shares nothing with the structure."""
    out: list[str] = []
    seen: set[str] = set()
    for el in scope.select_all("*")[:600]:
        text = " ".join(el.text.split())
        if not text or not _leaf(el):  # leaves only (no element children)
            continue
        sel = _candidates(el)[1] if len(_candidates(el)) > 1 else el.tag
        if sel in seen:
            continue
        seen.add(sel)
        out.append(f"{sel} ({text[:40]!r})")
        if len(out) >= limit:
            break
    return out


def _selector_hint(scope: "Document | Element | None", css: str, where: str) -> str:
    """One line naming the closest selectors to ``css`` in ``scope`` (or what the scope holds)."""
    if scope is None or not css:
        return ""
    close = suggest_selectors(scope, css)
    if close:
        return f" Closest selectors in {where}: " + ", ".join(close) + "."
    leaves = leaf_selectors(scope)
    return (
        f" Nothing in {where} matches those tokens; its text-bearing elements are: "
        + ", ".join(leaves)
        + "."
        if leaves
        else ""
    )


def _attr_hint(scope: "Document | Element | None", chain: str) -> str:
    """When a chain's selector MATCHED but its ``attr`` read nothing: the matched element's
    attributes and text, so the model reads one that exists."""
    m = _SELECT_ARG.search(chain)
    if scope is None or m is None:
        return ""
    el = scope.select(m.group(2))
    if el is None:
        return ""
    attrs = ", ".join(f"{k}={v[:40]!r}" for k, v in el.attrs.items() if k != "style")
    text = " ".join(el.text.split())[:80]
    return (
        f" The selector matched a <{el.tag}> whose attributes are: {attrs or '(none)'}; its text: "
        f"{text!r}. Read one of those with .attr('<name>') / .attr('text')."
    )


def _regions_line(doc: Document) -> str:
    """The page's detected REPEATING REGIONS (the parse layer's record detector) -- ground truth to
    pick records(...) from, instead of a guessed class."""
    regions = doc.records(top_k=4) if doc.kind != "json" else []
    if not regions:
        return ""
    return " REPEATING REGIONS detected on this page: " + ", ".join(
        f"{r.item_selector} ({r.count} items)" for r in regions
    )


def miss_pattern(doc: Document, records: str, missed: str, skip: int) -> str:
    """Which of the probed records DO contain the missed selector -- a miss on some of them means the
    record selector includes non-records (a table header row, an ad), not that the field is
    gone."""
    els = doc.select_all(records)[skip : skip + _PROBE_ROWS]
    hits = [i + skip + 1 for i, el in enumerate(els) if el.select(missed) is not None]
    if not els or not hits:
        return ""
    misses = [i + skip + 1 for i, el in enumerate(els) if el.select(missed) is None]
    return (
        f" It matched on record(s) {hits} but not {misses}: your records(...) selector includes "
        "non-records (a table header row? a featured item?) -- narrow it to the data rows (a class "
        "they share, 'tbody tr', ...) or keep only records with the field via where(...)."
    )


def _json_kind(value: object) -> str:
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "list"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (int, float)):
        return "number"
    return "null" if value is None else "str"


def _json_keys_hint(session: StepSession, op: Op) -> str:
    """On a JSON document: the keys the (first) record actually has -- what an ``attr`` could read
    -- so a key name is never guessed twice."""
    doc = session.detail_doc if op.name == "detail_field" else session.doc
    if doc is None or doc.kind != "json":
        return ""
    value = (
        doc.at(session.draft.records)
        if (op.name == "field" and session.draft.records)
        else doc.json()
    )
    first = value[0] if isinstance(value, list) and value else value
    if not isinstance(first, dict):
        return ""
    keys = ", ".join(f"{k} ({_json_kind(v)})" for k, v in list(first.items())[:40])
    return f" The record's keys are: {keys}. Read one of those with .attr('<key>')."


def _scope(session: StepSession, op: Op) -> "Document | Element | None":
    """Where a column's selector is evaluated: the FIRST record (field), or the sampled detail
    page (detail_field)."""
    if op.name == "detail_field":
        return session.detail_doc
    return session.doc.select(session.draft.records) if session.draft.records else None


def _dig(row: object, name: str) -> object:
    cur = row
    for part in name.split("."):
        cur = cur.get(part) if isinstance(cur, dict) else None
    return cur


def _short(value: object) -> str:
    text = json.dumps(value, ensure_ascii=False, default=str)
    return text if len(text) <= _VALUE_CHARS else text[: _VALUE_CHARS - 1] + "…"


def _empty(value: object) -> bool:
    if isinstance(value, list):
        return all(_empty(v) for v in value)
    return value in (None, "", {})


def _values(draft: Draft, rows: "list[object]") -> str:
    """Each column's values on the probed records -- the evidence the next op is chosen from."""
    if not draft.names():
        return ""
    lines = [f"Values on the first {len(rows)} record(s):"]
    for name in draft.names():
        vals = [_dig(r, name) for r in rows]
        empty = all(_empty(v) for v in vals)
        lines.append(
            f"  {name}: " + ", ".join(_short(v) for v in vals) + ("   ← EMPTY" if empty else "")
        )
    return "\n".join(lines)


# -- the loop ------------------------------------------------------------------------------------


def _turn(session: StepSession, result: str, brief: DatasetBrief) -> str:
    """One follow-up turn: the last op's result, the query so far, what is still to add."""
    have = set(session.draft.fields) | set(session.draft.detail_fields) | session.absent
    for d in session.finished:
        have |= set(d.fields) | set(d.detail_fields)
    required = [f for f in brief.fields if f not in have and f not in brief.optional]
    optional = [f for f in brief.fields if f not in have and f in brief.optional]
    so_far = session.draft.source() or '(nothing yet -- start with records("<css>"))'
    if session.finished:
        so_far = "\n".join(
            [f"SECTION {i + 1} (finished): {d.source()}" for i, d in enumerate(session.finished)]
            + [f"SECTION {len(session.finished) + 1} (current): {so_far}"]
        )
    parts = [result, "QUERY SO FAR:\n" + so_far]
    if required:
        parts.append(
            "STILL TO ADD (required): "
            + ", ".join(required)
            + " -- from the structure shown; a field that is NOT there (nor on the detail page) "
            "is absent(<name>), never a guessed selector."
        )
    if optional:
        parts.append("still to add (optional): " + ", ".join(optional))
    if session.absent:
        parts.append("declared absent: " + ", ".join(sorted(session.absent)))
    if not required and session.draft.records:
        parts.append(
            "Every required field is in the query -- "
            + (
                "declare the identity the brief asks for with identity(<field>, ...) and/or "
                "detail_identity(<stable css>), then "
                if brief.identity_hint
                and session.draft.identity is None
                and session.draft.detail_identity is None
                else ""
            )
            + "reply done() if the values above are right, else fix a column (field(...) "
            "replaces it)."
        )
    if session.offer_sibling:
        parts.append(
            "If the MISSING records live on a SEPARATE sibling page (a different URL), reply with "
            "EXACTLY `SIBLING: <that full url>` instead of an op."
        )
    parts.append("Reply with exactly ONE op call.")
    return "\n\n".join(parts)


async def _send(session: StepSession, llm: Llm) -> str:
    """One turn -> the raw reply. Conversational: the opening once, then the pending turn.
    Stateless: opening + the step history + the pending turn, every time."""
    if session.conv is None and isinstance(llm, Conversational):
        session.conv = llm.conversation()
    if session.conv is not None:
        text = session.opening if not session.opened else session.pending
        session.opened = True
        return await session.conv.send(text)
    session.opened = True
    prompt = session.opening
    if session.history:
        prompt += "\n\n---\nSTEPS SO FAR:\n" + "\n".join(session.history)
    if session.pending:
        prompt += "\n\n---\n" + session.pending
    return await llm.complete(prompt)


def _tally(session: StepSession, verbs: "Sequence[str]", unknown: "Sequence[str]") -> None:
    session.verbs.update(verbs)
    for v in unknown:
        if v not in session.unknown:
            session.unknown.append(v)


def _observe_with(llm: Llm) -> "Callable[[StepSession], Awaitable[_Obs]]":
    """The observe step bound to the model: send the turn, parse ONE op (a ``SIBLING:`` line when
    offered, or a rejection the next turn reports). A repeat of an op already tried is refused
    with that op's earlier result -- the model must do something different."""

    async def observe(session: StepSession) -> _Obs:
        if not session.draft.records and session.zero_picks >= _MAX_ZERO_PICKS:
            return _Obs(stop=True)  # three record selectors matched nothing: a shell page
        reply = await _send(session, llm)
        stripped = reply.strip()
        if session.offer_sibling and stripped.upper().startswith("SIBLING:"):
            url = stripped.split(":", 1)[1].strip().split()[0] if ":" in stripped else ""
            if url.startswith(("http://", "https://")):
                session.sibling = url
                return _Obs(sibling=url)
        try:
            op = parse_op(reply)
        except StepError as exc:
            _tally(session, exc.verbs, exc.unknown)
            # the FULL reply, so a human can see WHY it was not an op (never a snippet)
            return _Obs(error=f"{exc}\n  the reply was: {' '.join(stripped.split())!r}")
        _tally(session, op.verbs, ())
        if op.name == "done" and not session.draft.records:
            return _Obs(error="done() before any records(...) -- nothing to finish")
        if op.name != "done" and op.line() in session.tried:
            earlier = session.tried[op.line()]
            if op.name in ("detail", "absent", "section"):  # already DONE -- move on
                return _Obs(
                    error=f"{op.line()} is already done ({earlier}) -- continue with the next op "
                    "(detail_field(...) reads the detail page; done() finishes)."
                )
            return _Obs(
                error=f"you already called exactly {op.line()} -- its result was: {earlier}. Do "
                "something DIFFERENT (another selector, an attribute read, detail(...), or "
                "absent(<name>) if the field is not there)."
            )
        if op.name == "done":
            session.done = True
        return _Obs(op=op)

    return observe


def _decide(obs: _Obs) -> "Op | Done":
    if obs.stop or obs.sibling or (obs.op is not None and obs.op.name == "done"):
        return Done()
    if obs.op is None:
        return Op("bad", [obs.error])
    return obs.op


def _apply_with(
    brief: DatasetBrief, resolver: Resolver
) -> "Callable[[StepSession, Op | Done], Awaitable[None]]":
    """The apply step bound to the brief + resolver: probe the op, record it, write the next turn."""

    async def apply(session: StepSession, op: "Op | Done") -> None:
        if isinstance(op, Done):  # never reached (the loop stops on Done) -- typed for the seam
            return
        if op.name == "bad":
            session.pending = _turn(session, f"NOT APPLIED: {op.args[0]}", brief)
            emit(ReasonEvent(stage="author", text=f"step rejected: {op.args[0]}"))
            return
        applied, result, summary = await _step(session, op, resolver)
        if not summary.startswith(_PRECONDITION):  # a precondition can change -- do not memorise
            session.tried[op.line()] = summary
        if applied:
            session.steps += 1
            session.history.append(f"{op.line()} -> {summary}")
            emit(ReasonEvent(stage="author", text=f"step {session.steps}: {op.line()} → {summary}"))
        else:
            emit(ReasonEvent(stage="author", text=f"step not applied: {op.line()} → {summary}"))
        session.pending = _turn(session, result, brief)

    return apply


async def _step(session: StepSession, op: Op, resolver: Resolver) -> "tuple[bool, str, str]":
    """Apply ``op`` to a COPY of the draft and probe it -> ``(applied, the result turn text, a
    one-line summary)``. A failed probe leaves the draft untouched (the op is reverted)."""
    doc, new = session.doc, session.draft.copy()
    head = f"RESULT of {op.line()}:"
    if op.name == "absent":
        session.absent.add(op.args[0])
        new.fields.pop(op.args[0], None)
        new.detail_fields.pop(op.args[0], None)
        session.draft = new
        return (
            True,
            f"{head}\n{op.args[0]} recorded as ABSENT from this source (it will be reported, "
            "not guessed).",
            f"{op.args[0]} declared absent",
        )
    if op.name in ("identity", "detail_identity"):
        if not (new.records and new.columns()):
            return False, f"{head}\nNOT APPLIED -- extract the fields first", "no fields yet"
        if op.name == "detail_identity" and not new.link:
            return (
                False,
                f'{head}\nNOT APPLIED -- call detail("<link css>") first',
                "no detail link",
            )
        if op.name == "identity":
            new.identity = list(op.args)
        else:
            new.detail_identity = list(op.args)
        try:
            probed = await _probe(new, doc, resolver, skip=session.skip)
        except Exception as exc:  # a selector part that raises: this op failed
            return False, f"{head}\nFAILED -- {exc}. The op was REVERTED.", f"reverted ({exc})"
        session.draft = new
        key = "_identity" if op.name == "identity" else f"{DETAIL_COLUMN}._identity"
        ids = [_dig(r, key) for r in probed]
        distinct = len({str(i) for i in ids})
        return (
            True,
            f"{head}\nidentity declared over {op.args or ['every field']}: {distinct} distinct "
            f"identit{'y' if distinct == 1 else 'ies'} across the {len(probed)} probed record(s)"
            + (
                " -- WARNING: not unique per record; add a distinguishing part."
                if distinct < len(probed)
                else ""
            ),
            f"{distinct}/{len(probed)} distinct",
        )
    if op.name == "section":
        if not (session.draft.records and session.draft.columns()):
            return (
                False,
                f"{head}\nNOT APPLIED -- finish the current section first (records + at least one "
                "field) before starting another.",
                "current section unfinished (not applied)",
            )
        probe = Draft(records=op.args[0])
        try:
            n = await _count(probe, doc, resolver)
        except Exception as exc:  # as for records(...)
            return False, f"{head}\nNOT APPLIED -- {exc}", f"not applied ({exc})"
        if n == 0:
            return (
                False,
                f"{head}\nNOT APPLIED -- {op.args[0]!r} matched 0 elements.",
                "matched 0 elements (not applied)",
            )
        session.finished.append(session.draft)
        session.draft = probe
        return (
            True,
            f"{head}\nSECTION {len(session.finished) + 1} started: matched {n} record(s). The FIRST "
            "record's structure (wq.doc for field(...)):\n"
            + (_first_record(doc, probe.records) or "(not a markup record)")
            + "\nAdd this section's fields (the same columns as the other section), then done().",
            f"section {len(session.finished) + 1}: matched {n} record(s)",
        )
    if op.name == "records":
        new.records = op.args[0]
        try:
            n = await _count(new, doc, resolver)
        except Exception as exc:  # a bad selector can raise anything from the parser: not applied
            return False, f"{head}\nNOT APPLIED -- {exc}", f"not applied ({exc})"
        if n == 0:
            session.zero_picks += 1
            return (
                False,
                f"{head}\nNOT APPLIED -- {op.args[0]!r} matched 0 elements. Pick the repeating "
                "element from the skeleton (the one marked ← RECORD LIST is the likely row)."
                + (
                    f" LOCATE detected the record list at {session.record_selector!r}."
                    if session.record_selector
                    else ""
                )
                + _selector_hint(doc, op.args[0], "the page")
                + _regions_line(doc),
                "matched 0 elements (not applied)",
            )
        rows: list[object] = []
        _index, skip = _typical(doc, new.records)
        if new.columns():
            try:
                rows = await _probe(new, doc, resolver, skip=skip)
            except Exception as exc:  # the existing columns failed on the new records: not applied
                return False, f"{head}\nNOT APPLIED -- with this record selector {exc}", "failed"
        session.draft = new
        session.skip = skip
        session.zero_picks = 0
        text = f"{head}\nmatched {n} record(s). The FIRST record's structure (wq.doc for field(...)):\n"
        text += _first_record(doc, new.records) or "(not a markup record)"
        if session.record_selector and new.records != session.record_selector:
            text += (
                f"\n(note: the page analysis detected the record list at "
                f"{session.record_selector!r} -- if these {n} matches include non-records, "
                "re-pick with that.)"
            )
        if rows:
            text += "\n\n" + _values(new, rows)
        return True, text, f"matched {n} record(s)"
    if op.name == "detail":
        if not new.records:
            return False, f"{head}\nNOT APPLIED -- pick records(...) first", "no records yet"
        href = _typical_link(_record_links(doc, new.records, op.args[0]))
        if href is None:
            return (
                False,
                f"{head}\nNOT APPLIED -- no link {op.args[0]!r} in the first records (or it has no "
                "href). Name the record's link element as shown in its structure.",
                "no such link in the records (not applied)",
            )
        try:
            page = await resolver.resolve(href)
        except WebException as exc:
            return False, f"{head}\nNOT APPLIED -- fetching {href} failed: {exc}", "fetch failed"
        new.link = op.args[0]
        session.draft = new
        session.detail_doc = page
        skel = clip(skeleton_for(page), _DETAIL_CHARS, "detail skeleton", kind="html")
        return (
            True,
            f"{head}\nfollowed a typical record's link, {href} ({page.kind}). The DETAIL page's "
            f"structure (wq.doc for detail_field(...)):\n{skel}",
            f"followed {href}",
        )
    if op.name == "detail_field" and not new.link:
        return False, f'{head}\nNOT APPLIED -- call detail("<link css>") first', "no detail link"
    if op.name == "field":  # a column name is unique across the listing / detail scopes
        new.fields[op.args[0]] = op.args[1]
        new.detail_fields.pop(op.args[0], None)
    elif op.name == "detail_field":
        new.detail_fields[op.args[0]] = op.args[1]
        new.fields.pop(op.args[0], None)
    elif op.name == "where":
        new.where = op.args[0]
    elif op.name == "drop":
        new.fields.pop(op.args[0], None)
        new.detail_fields.pop(op.args[0], None)
    if not new.records:
        return False, f"{head}\nNOT APPLIED -- pick records(...) first", "no records yet"
    try:
        rows = await _probe(new, doc, resolver, skip=session.skip)
    except WebException as exc:
        missed = exc.error.detail.get("selector") if exc.error.code == "dsl.select_miss" else None
        where = "the detail page" if op.name == "detail_field" else "the record"
        hint = _selector_hint(_scope(session, op), str(missed or ""), where)
        if isinstance(missed, str) and op.name == "field":
            hint += miss_pattern(doc, new.records, missed, session.skip)
        return (
            False,
            f"{head}\nFAILED -- {exc.error.code}: {exc.error.message}. The op was REVERTED. A "
            "selector must match inside EVERY record; a field absent on some records is read with "
            f"select(css, optional=True).{hint}",
            f"reverted ({exc.error.code}: {exc.error.message})",
        )
    except asyncio.TimeoutError:
        return (
            False,
            f"{head}\nFAILED -- the probe exceeded {_PROBE_TIMEOUT:.0f}s and was REVERTED.",
            "reverted (timeout)",
        )
    except Exception as exc:  # a model-written predicate/chain crashed the DSL: THIS op failed
        return (
            False,
            f"{head}\nFAILED -- running it raised {type(exc).__name__}: {exc}. The op was "
            "REVERTED; write it differently (see the guide for the exact verb signatures).",
            f"reverted ({type(exc).__name__}: {exc})",
        )
    col = op.args[0]
    key = f"{DETAIL_COLUMN}.{col}" if op.name == "detail_field" else col
    if op.name in ("field", "detail_field"):
        vals = [_dig(r, key) for r in rows]
        # a column that reads EMPTY on every probed record is not extracted data: revert it (an
        # optional select that legitimately misses is the one exception) so the draft never
        # degrades and the model must try a different read -- or declare the field absent.
        if all(_empty(v) for v in vals) and "optional=True" in op.args[1]:
            # an optional read may legitimately miss a few records -- look wider before deciding
            wider = await _probe(new, doc, resolver, skip=session.skip, rows=_PROBE_ROWS_OPTIONAL)
            if all(_empty(_dig(r, key)) for r in wider):
                return (
                    False,
                    f"{head}\nEMPTY on every one of {len(wider)} probed records although optional "
                    "-- REVERTED: optional=True is for a field SOME records lack, not for a selector "
                    "that matches nothing. Pick the element from the record structure, or say "
                    "absent(<name>).",
                    f"EMPTY on all {len(wider)} probed records (reverted)",
                )
        if all(_empty(v) for v in vals) and "optional=True" not in op.args[1]:
            raw = await _raw_values(new, op, doc, resolver)
            if raw:  # the selector DID match -- the transform threw the value away
                return (
                    False,
                    f"{head}\nEMPTY on every probed record -- REVERTED. The selector matched, but "
                    f"the transform produced nothing. The RAW text it read was: {raw}. FIRST look "
                    "in the record structure for the element or attribute that holds JUST this "
                    "value (a <time datetime=...>, a data-* attribute, a smaller span) and select "
                    "that; only if the value has no element of its own, pull it out of that text "
                    "with .regex(pattern, group=1) before the transform.",
                    "EMPTY after the transform (reverted)",
                )
            scope = _scope(session, op)
            hint = _attr_hint(scope, op.args[1]) or _json_keys_hint(session, op)
            if not hint:
                m = _SELECT_ARG.search(op.args[1])
                where = "the detail page" if op.name == "detail_field" else "the record"
                hint = _selector_hint(scope, m.group(2) if m else "", where)
            return (
                False,
                f"{head}\nEMPTY on every probed record -- REVERTED. The selector matched nothing "
                "inside the record, or the value is in an ATTRIBUTE (read it with .attr('<name>'))."
                f"{hint} If the field is genuinely not there, say absent(<name>).",
                "EMPTY on every record (reverted)",
            )
    session.draft = new
    if op.name in ("field", "detail_field"):
        session.absent.discard(col)  # a field now read is no longer "absent"
    values = _values(new, rows)
    summary = (
        f"{col} dropped"
        if op.name == "drop"
        else (
            f"{len(rows)} record(s) kept"
            if op.name == "where"
            else f"{key}: " + ", ".join(_short(_dig(r, key)) for r in rows)
        )
    )
    return True, f"{head}\n{values or f'{len(rows)} record(s)'}", summary


def max_steps(brief: DatasetBrief) -> int:
    """The step budget for one run: room for every field plus re-picks, bounded."""
    return min(24, max(8, 4 + 2 * len(brief.fields)))


async def run_steps(
    session: StepSession,
    *,
    reference: Reference,
    brief: DatasetBrief,
    resolver: Resolver,
    llm: Llm,
    flags: "list[Flag]",
    recency: str = "",
    note: str = "",
    budget: int = 0,
) -> StepResult:
    """Drive the step engine until ``done()`` (or its budget) and return the :class:`StepResult`
    -- the draft rerooted at the source as a runnable query. ``note`` is the outer loop's follow-up
    (a failure / a detail-page hint / a sibling offer) and becomes the next turn; the draft carries
    over. ``flags`` / ``recency`` render the opening on the first run. Detail pages fetched by the
    probes are memoised for the whole run (one fetch per URL, not one per step)."""
    session.record_selector = reference.record_selector or ""
    if not session.opening:
        session.opening = steps_prompt(
            brief,
            skeleton_for(session.doc),
            flags,
            kind=session.doc.kind,
            recency=recency,
            record_selector=reference.record_selector or "",
            regions=_regions_line(session.doc),
        )
    session.done = False
    session.sibling = ""
    if note:
        session.pending = _turn(session, note, brief)
    elif not session.pending and session.opened:
        session.pending = _turn(session, "Continue.", brief)
    loop: "BoundedLoop[StepSession, _Obs, Op | Done]" = BoundedLoop(
        observe=_observe_with(llm),
        decide=_decide,
        apply=_apply_with(brief, resolver),
        done=lambda d: isinstance(d, Done),
        progress=lambda s: s.steps,  # a rejected / reverted op is no progress -> stall-bounded
        max_rounds=budget or max_steps(brief),
        max_stalls=_MAX_STALLS,
    )
    with resolve_memo():
        verdict = await loop.arun(session)
    if session.sibling:
        return StepResult(sibling=session.sibling)
    if not session.draft.records and session.zero_picks >= _MAX_ZERO_PICKS:
        emit(
            ReasonEvent(
                stage="author",
                text=f"{session.zero_picks} record selectors matched nothing on the fetched page "
                "— the records are not in this HTML (a shell: JS / interaction-gated beyond a "
                "plain render); stopping instead of guessing",
            )
        )
        return StepResult(
            error="no record selector matches anything on the fetched page", no_records=True
        )
    if not session.draft.records:
        return StepResult(
            error=f"the step-by-step build produced no record selector ({verdict.reason})",
            hint='Start with records("<css>") naming the repeating element from the skeleton.',
            absent=set(session.absent),
        )
    profile = reference.profile or None
    query = reroot(parse_query(session.draft.source()), reference.url, profile=profile)
    sections: list[tuple[Query, list[object]]] = []
    with resolve_memo():
        for d in session.finished:  # the earlier sections, tested over the same fetched page
            rows: list[object] = []
            try:
                got = await asyncio.wait_for(
                    parse_query(d.source()).acollect(session.doc, resolver=resolver),
                    timeout=_PROBE_TIMEOUT,
                )
                rows = list(got) if isinstance(got, list) else [got]
            except (WebException, asyncio.TimeoutError) as exc:
                emit(ReasonEvent(stage="author", text=f"section {d.source()} failed: {exc}"))
            sections.append((reroot(parse_query(d.source()), reference.url, profile=profile), rows))
    # the STRUCTURAL verbs the finished query uses (the tally so far counts the chains the model
    # wrote for columns / predicates) -- so the record reads as the whole query's verb use.
    for d in (*session.finished, session.draft):
        structural = (
            ["select_all"] + (["filter"] if d.where else []) + (["extract"] if d.columns() else [])
        )
        if d.link and d.detail_fields:
            structural += ["select", "attr", "resolve", "extract"]
        _tally(session, structural, ())
    remark = (
        ""
        if session.done
        else f"the step loop stopped ({verdict.reason}"
        + (f": {verdict.error}" if verdict.error else "")
        + ") — taking the draft so far"
    )
    if remark:
        emit(ReasonEvent(stage="author", text=remark))
    return StepResult(query=query, note=remark, absent=set(session.absent), sections=sections)


__all__ = [
    "OPS",
    "Draft",
    "Op",
    "StepError",
    "StepResult",
    "StepSession",
    "leaf_selectors",
    "max_steps",
    "miss_pattern",
    "parse_op",
    "run_steps",
    "suggest_selectors",
]
