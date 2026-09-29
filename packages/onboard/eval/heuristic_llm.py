"""A deterministic stand-in for the onboard tier's model, for offline eval.

With no ``ANTHROPIC_API_KEY``, the eval still needs an :class:`~web.onboard.Llm` that turns the
author prompt into a ``wq`` chain. :class:`HeuristicLlm` does what a real model does -- it READS the
prompt (the page skeleton + the requested fields) and writes the query -- but by a fixed heuristic
rather than by understanding. It therefore measures the PIPELINE MECHANICS (prompt -> parse ->
reroot -> run) and how far a mechanical author gets, NOT a model's selector quality; a real
``AnthropicLlm`` is a drop-in replacement when a key is present.

The heuristic: find the highest-count ``select_all(...)`` record region marked in the skeleton,
read one record's child lines, and map each requested field to a child (preferring a leaf) by a
class / tag / attribute whose name matches; a plural field over a nested marked list becomes a
``select_all`` (a JSON list); an HTML table maps fields to ``td:nth-of-type(k)`` by header; JSON is
the array path + key names. It deliberately does NOT special-case any fixture.
"""

from __future__ import annotations

import re

_NOISE = frozenset({"th", "option", "meta", "link", "script"})
_LINKISH = frozenset({"url", "link", "href", "detail", "source", "page", "profile"})
_DATEISH = frozenset({"date", "published", "updated", "when", "time", "timestamp", "age"})

_MARK = re.compile(r'RECORD LIST · (\d+) · select_all\("([^"]+)"\)')
_SUBMARK = re.compile(r'select_all\("([^"]+)"\)')
_ELEM = re.compile(r"^([A-Za-z][\w-]*)((?:\.[\w-]+)+)?")
_ATTRS = re.compile(r"\[([\w-]+)(?:=([^\]]*))?\]")


class _Line:
    __slots__ = ("indent", "tag", "classes", "attrs", "is_link", "sub", "text")

    def __init__(
        self,
        indent: int,
        tag: str,
        classes: list[str],
        attrs: list[str],
        is_link: bool,
        sub: str,
        text: str,
    ) -> None:
        self.indent = indent
        self.tag = tag
        self.classes = classes
        self.attrs = attrs
        self.is_link = is_link
        self.sub = sub  # a nested select_all(...) selector marked on this line, else ""
        self.text = text  # the element's shown text (e.g. a header cell label), else ""

    def css(self) -> str:
        return f"{self.tag}.{self.classes[0]}" if self.classes else self.tag


def _parse_line(raw: str) -> "_Line | None":
    indent = len(raw) - len(raw.lstrip(" "))
    body = raw.strip()
    m = _ELEM.match(body)
    if m is None:
        return None
    classes = [c for c in (m.group(2) or "").split(".") if c]
    attrs = [a for a, _v in _ATTRS.findall(body)]
    sub = _SUBMARK.search(body)
    quoted = re.search(r"'([^']*)'", body)
    return _Line(
        indent,
        m.group(1),
        classes,
        attrs,
        m.group(1) == "a" or "(href)" in body,
        sub.group(1) if sub else "",
        quoted.group(1) if quoted else "",
    )


def _section(prompt: str, start: str, end: str) -> str:
    i = prompt.find(start)
    j = prompt.find(end, i + 1) if i != -1 else -1
    return prompt[i + len(start) : j] if i != -1 and j != -1 else ""


def _fields(prompt: str) -> list[str]:
    for line in prompt.splitlines():
        if line.startswith("Fields (") and "): " in line:
            spec = line.split("): ", 1)[1]
            return [p.split(" [", 1)[0].strip() for p in spec.split(", ") if p.strip()]
    return []


def _record_selector(skeleton: str) -> str:
    best, best_n = "", -1
    for n, sel in _MARK.findall(skeleton):
        if sel.split()[-1].split(".")[0] in _NOISE:
            continue
        if int(n) > best_n:
            best, best_n = sel, int(n)
    return best


def _lines(skeleton: str) -> list[_Line]:
    return [pl for pl in (_parse_line(raw) for raw in skeleton.splitlines()) if pl is not None]


def _record_lines(all_lines: list[_Line], selector: str) -> list[_Line]:
    tag = selector.split()[-1].split(".")[0]
    cls = selector.split(".")[1] if "." in selector.split()[-1] else ""
    for i, pl in enumerate(all_lines):
        if pl.tag == tag and (not cls or cls in pl.classes):
            kids: list[_Line] = []
            for nxt in all_lines[i + 1 :]:
                if nxt.indent <= pl.indent:
                    break
                kids.append(nxt)
            return kids
    return []


def _pick(field: str, kids: list[_Line]) -> "_Line | None":
    low = field.lower()
    exact = [k for k in kids if low in [c.lower() for c in k.classes]]
    partial = sorted(
        (k for k in kids if any(low in c.lower() for c in k.classes)),
        key=lambda k: k.indent,
        reverse=True,
    )  # deepest (leaf-most) first
    if low in _LINKISH:
        links = [k for k in kids if k.is_link]
        if links:
            return links[0]
    heading = (
        [k for k in kids if k.tag in ("h1", "h2", "h3", "h4")]
        if low in ("title", "name", "heading")
        else []
    )
    tagmatch = [k for k in kids if k.tag == low]
    hits = exact or partial or heading or tagmatch
    return hits[0] if hits else None


def _column(field: str, line: "_Line") -> str:
    low = field.lower()
    if line.sub:  # a nested marked list -> a JSON list of values
        acc = "href" if low in _LINKISH else "text"
        return f'{field}=wq.doc.select_all("{line.sub}").attr("{acc}")'
    # every field select is optional=True: loud-by-default select would RAISE on any record that
    # lacks the field, aborting the whole extraction -- a mechanical author cannot know which fields
    # are on every row, so it tolerates a miss (-> null) per column.
    if low in _LINKISH and line.is_link:
        return f'{field}=wq.doc.select("{line.css()}", optional=True).attr("href")'
    if low in _DATEISH and "datetime" in line.attrs:
        return f'{field}=wq.doc.select("{line.css()}", optional=True).attr("datetime")'
    if low == "rating" and line.classes:  # a value carried in a class token
        return f'{field}=wq.doc.select("{line.css()}", optional=True).attr("class")'
    return f'{field}=wq.doc.select("{line.css()}", optional=True).attr("text")'


def _html_query(record: str, fields: list[str], all_lines: list[_Line]) -> str:
    kids = _record_lines(all_lines, record)
    is_table = record.split()[-1] == "tr"  # a plain header-column table (not tr.someclass)
    headers = [ln.text for ln in all_lines if ln.tag == "th"]
    cols: list[str] = []
    for i, f in enumerate(fields):
        line = _pick(f, kids)
        if line is not None and not (is_table and not line.classes):
            cols.append(_column(f, line))
        elif is_table:  # a header-column table: map the field to its column
            col = next(
                (h + 1 for h, label in enumerate(headers) if f.lower() in label.lower()),
                i + 1,
            )
            cols.append(f'{f}=wq.doc.select("td:nth-of-type({col})", optional=True).attr("text")')
    if not cols:
        cols.append('text=wq.doc.attr("text")')
    root = "tbody tr" if is_table else record
    return f'wq.doc.select_all("{root}").extract({", ".join(cols)})'


# -- JSON ---------------------------------------------------------------------


def _json_path(skeleton: str) -> "tuple[str, list[str]]":
    """The FULLY-QUALIFIED dotted path to the record array and its object's scalar keys."""
    lines = skeleton.splitlines()
    stack: list[tuple[int, str]] = []  # (indent, key) ancestry of open objects
    for i, raw in enumerate(lines):
        indent = len(raw) - len(raw.lstrip(" "))
        while stack and indent <= stack[-1][0]:
            stack.pop()
        arr = re.match(r"\s*([\w]+): \[\d+\]", raw)
        if arr is not None:
            path = ".".join([k for _, k in stack] + [arr.group(1)])
            return path, _json_keys(lines, i, indent)
        obj = re.match(r"\s*([\w]+): \{", raw)
        if obj is not None:
            stack.append((indent, obj.group(1)))
    return "", []


def _json_keys(lines: list[str], start: int, arr_indent: int) -> list[str]:
    keys: list[str] = []
    prefix = ""
    prefix_indent = -1
    for raw in lines[start + 1 :]:
        indent = len(raw) - len(raw.lstrip(" "))
        if raw.strip() and indent <= arr_indent:
            break
        if prefix and indent <= prefix_indent:
            prefix = ""
        scalar = re.match(r"\s*(\w+): (number|string|bool)", raw)
        obj = re.match(r"\s*(\w+): \{", raw)
        if scalar is not None:
            keys.append(prefix + scalar.group(1))
        elif obj is not None:
            prefix, prefix_indent = obj.group(1) + ".", indent
    return keys


def _json_query(fields: list[str], path: str, keys: list[str]) -> str:
    cols: list[str] = []
    for f in fields:
        match = next((k for k in keys if k.split(".")[-1].lower() == f.lower()), None)
        if match is None:
            continue
        cols.append(
            f'{f}=wq.doc.select("{match}").attr("text")'
            if "." in match
            else f'{f}=wq.doc.attr("{match}")'
        )
    if not cols:
        cols = [f'{k.split(".")[-1]}=wq.doc.attr("{k}")' for k in keys if "." not in k]
    return f'wq.doc.select_all("{path}").extract({", ".join(cols)})'


class HeuristicLlm:
    """An :class:`~web.onboard.Llm` that authors a ``wq`` chain from the prompt's skeleton + fields
    by a fixed heuristic (no model). Records the last prompt + reply for the report."""

    def __init__(self) -> None:
        self.prompt = ""
        self.reply = ""

    async def complete(self, prompt: str) -> str:
        self.prompt = prompt
        fields = _fields(prompt)
        skeleton = _section(prompt, "row selector):\n", "\n\nReply with ONLY")
        if (
            "This is a JSON document" in prompt
        ):  # the author_prompt kind-note (not the guide's prose)
            path, keys = _json_path(skeleton)
            self.reply = _json_query(fields, path, keys) if path else "wq.doc"
            return self.reply
        record = _record_selector(skeleton)
        self.reply = _html_query(record, fields, _lines(skeleton)) if record else "wq.doc"
        return self.reply


__all__ = ["HeuristicLlm"]
