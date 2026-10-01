"""A GENERIC brief with arguments -- the dataset ask every stage reads.

A brief is markdown with YAML frontmatter (``briefs/<name>.md``, or any file). It declares the
``args`` it accepts and templates them anywhere with ``{name}`` (``{company} investor relations
news``); :meth:`Brief.render` substitutes them in EVERY string of the brief and fails loudly on a
missing one, so a stage never sees a half-templated prompt. The ``search`` section is what the
deterministic search stage scores by; ``look`` / ``ignore`` are the one-line scope the review
stages show the model; ``schema`` is the field list the author extracts.
"""

from __future__ import annotations

from importlib.resources import files
from pathlib import Path
from string import Formatter

from pydantic import BaseModel, JsonValue
from web.dsl import FieldDef, Schema, parse_range

from ..models import _parse_frontmatter, _schema


class BriefError(ValueError):
    """The brief cannot be rendered / loaded -- an argument it declares is missing, or an unknown
    one was passed; the message names it."""


class SearchSpec(BaseModel):
    """What the search stage does, deterministically: run ``term`` for up to ``k`` results and
    score each by how many ``domain`` / ``path`` hint fragments its URL carries."""

    term: str = ""
    k: int = 10
    domain: list[str] = []  # host fragments: "{company}", "investors.{company}", "q4cdn"
    path: list[str] = []  # path fragments: "news", "press", "investor"
    max_pages: int = 12  # the crawl bound (stage 3)


#: a brief field IS the DSL's field definition (name / type / description / optional).
FieldSpec = FieldDef


class Brief(BaseModel):
    """The onboarding ask. ``goal`` is the dataset in one paragraph; ``title`` names it; ``args``
    are the names a caller supplies; ``search`` drives stage 1; ``look`` / ``ignore`` scope the
    reviews; ``fields`` are what the author extracts; ``expect_rows`` bounds a run ("10-50")."""

    name: str = ""
    title: str = ""
    goal: str = ""
    args: list[str] = []
    search: SearchSpec = SearchSpec()
    look: list[str] = []
    ignore: list[str] = []
    fields: list[FieldSpec] = []
    expect_rows: str = ""
    hints: dict[str, str] = {}  # per-stage natural-language hints: {"author_extract": "..."}
    values: dict[str, str] = {}  # the argument values once rendered

    # -- loading --

    @classmethod
    def from_markdown(cls, text: str) -> "Brief":
        front, body = _parse_frontmatter(text)
        data: dict[str, JsonValue] = {k: v for k, v in front.items() if k in cls.model_fields}
        data.setdefault("goal", body.strip())
        if "schema" in front:
            names, descriptions, types = _schema(front["schema"])
            optional = front.get("optional")
            opt = {str(o) for o in optional} if isinstance(optional, list) else set()
            data["fields"] = [
                {
                    "name": n,
                    "type": types.get(n, "string"),
                    "description": descriptions.get(n, ""),
                    "optional": n in opt,
                }
                for n in names
            ]
        return cls.model_validate(data)

    @classmethod
    def load(cls, spec: str) -> "Brief":
        """A brief from a FILE path or a PACKAGED name (``briefs/<name>.md``); else ``spec`` is
        the goal of an ad-hoc brief."""
        if Path(spec).is_file():
            return cls.from_markdown(Path(spec).read_text(encoding="utf-8"))
        for name in {spec, spec.replace("-", "_"), spec.replace("_", "-")}:
            res = files("web.onboard").joinpath(f"briefs/{name}.md")
            if res.is_file():
                return cls.from_markdown(res.read_text(encoding="utf-8"))
        return cls(goal=spec)

    # -- rendering --

    def render(self, **values: str) -> "Brief":
        """The brief with every ``{arg}`` substituted. Raises :class:`BriefError` for a declared
        argument that is missing or a value that is not declared."""
        unknown = sorted(set(values) - set(self.args))
        missing = sorted(set(self.args) - set(values))
        if unknown:
            raise BriefError(f"{self.name or 'the brief'} takes no argument {', '.join(unknown)}")
        if missing:
            raise BriefError(f"{self.name or 'the brief'} needs {', '.join(missing)}")
        data = self.model_dump()
        data["values"] = dict(values)
        return Brief.model_validate(_fill(data, values))

    def placeholders(self) -> "set[str]":
        """Every ``{name}`` the brief's strings reference (what ``args`` should declare)."""
        found: set[str] = set()
        _collect(self.model_dump(), found)
        return found

    # -- what stages read --

    @property
    def required(self) -> "list[str]":
        return [f.name for f in self.fields if not f.optional]

    @property
    def names(self) -> "list[str]":
        return [f.name for f in self.fields]

    def scope(self) -> str:
        """The one-line scope for a review prompt: what to look for, what to leave out."""
        parts = []
        if self.look:
            parts.append("LOOK FOR: " + "; ".join(self.look))
        if self.ignore:
            parts.append("LEAVE OUT: " + "; ".join(self.ignore))
        return "\n".join(parts)

    def schema_lines(self) -> str:
        """The field list as the author sees it: ``- name (type): description [optional]``."""
        return "\n".join(
            f"- {f.name} ({f.type}): {f.description}" + (" [optional]" if f.optional else "")
            for f in self.fields
        )

    def expected_range(self) -> "tuple[int, int] | None":
        return parse_range(self.expect_rows)

    def as_schema(self) -> Schema:
        """The brief as the query's OPTIONAL schema (rides the authored blob)."""
        return Schema(fields=list(self.fields), expect_rows=self.expect_rows)


def packaged_briefs() -> "list[str]":
    """The names of the briefs bundled with the package (``web/onboard/briefs/*.md``)."""
    root = files("web.onboard").joinpath("briefs")
    return sorted(p.name[:-3] for p in root.iterdir() if p.name.endswith(".md"))


_FMT = Formatter()


def _fill(value: JsonValue, values: "dict[str, str]") -> JsonValue:
    """``{name}`` substituted in every string of a JSON value (dicts / lists recursed)."""
    if isinstance(value, str):
        return value.format_map(values) if "{" in value else value
    if isinstance(value, list):
        return [_fill(v, values) for v in value]
    if isinstance(value, dict):
        return {k: _fill(v, values) for k, v in value.items()}
    return value


def _collect(value: JsonValue, found: "set[str]") -> None:
    if isinstance(value, str):
        for _lit, name, _spec, _conv in _FMT.parse(value):
            if name:
                found.add(name)
    elif isinstance(value, list):
        for v in value:
            _collect(v, found)
    elif isinstance(value, dict):
        for v in value.values():
            _collect(v, found)


__all__ = ["Brief", "BriefError", "FieldSpec", "SearchSpec", "packaged_briefs"]
