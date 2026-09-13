"""Regenerate the typed surface stubs from the cores + backing registry.

The surface classes dispatch via ``__getattr__`` at runtime and carry no op
methods; static checkers get their signatures from generated blocks that cannot
drift. Each block lives between ``>>> generated: ... <<<`` and ``>>> end
generated <<<`` markers and is rewritten here from a core's data fields
(``model_fields``) plus the op contract below, mapped into the eager tier
(Document / Field[T] / Collection[T]).

    python scripts/gen_stubs.py          # rewrite the blocks
    python scripts/gen_stubs.py --check  # exit 1 if any block is stale (CI)
"""

from __future__ import annotations

import sys
import types as _types
import typing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from webclient.core.document_core import DocumentCore  # noqa: E402
from webclient.core.reference_core import ReferenceCore  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SURFACES = ROOT / "webclient" / "surfaces.py"
COLLECTION = ROOT / "webclient" / "collection.py"
MODELS = ROOT / "webclient" / "models.py"

_SCALAR = {str: "str", int: "int", float: "float", bytes: "bytes", bool: "bool"}


def _type_of(ann: object) -> str:
    if ann in _SCALAR:
        return _SCALAR[ann]
    if typing.get_origin(ann) is typing.Literal:
        return "str"
    if typing.get_origin(ann) in (typing.Union, getattr(_types, "UnionType", None)):
        rest = [a for a in typing.get_args(ann) if a is not type(None)]
        if len(rest) == 1 and rest[0] in _SCALAR:
            return f"{_SCALAR[rest[0]]} | None"
    return "Any"


def _fields(core: type, skip: set[str]) -> list[str]:
    return [
        f"        {name}: {_type_of(f.annotation)}"
        for name, f in core.model_fields.items()
        if name not in skip
    ]


# The op contract: fully-rendered method/attribute lines. ``{D}``/``{R}``/
# ``{FS}``/``{FB}``/``{CD}``/``{CR}`` expand to the tier's type names.
_REFERENCE_OPS = [
    "@property",
    "def url(self) -> str: ...",
    "@property",
    "def ok(self) -> bool: ...",
    "def resolve(self, *, browser: bool = ..., optional: bool = ...,",
    '            error: Any = ...) -> "{D}": ...',
    'def with_params(self, **params: str) -> "{R}": ...',
    'def replace(self, **fields: Any) -> "{R}": ...',
    'def join(self, href: str) -> "{R}": ...',
]

_DOCUMENT_OPS = [
    "@property",
    "def ok(self) -> bool: ...",
    "@property",
    "def text(self) -> str: ...",
    "@property",
    "def title(self) -> str: ...",
    "@property",
    "def message(self) -> str: ...",
    "@property",
    "def events(self) -> list[Any]: ...",
    "@property",
    "def action_events(self) -> list[Any]: ...",
    "@property",
    "def dom_mutations(self) -> list[Any]: ...",
    "def select(self, selector: str, *, index: int = ...,",
    '           error: Any = ...) -> "{D}": ...',
    "def select_all(self, selector: str, *, limit: int | None = ...,",
    '               offset: int = ...) -> "{CD}": ...',
    "@overload",
    'def attr(self, name: Literal["href", "src", "action"]) -> "{R}": ...  # type: ignore[overload-overlap]',
    "@overload",
    'def attr(self, name: str, *, error: Any = ...) -> "{FS}": ...',
    'def is_ok(self) -> "{FB}": ...',
    'def is_empty(self) -> "{FB}": ...',
    'def ref(self) -> "{R}": ...',
    "def events_of(self, event_type: Any) -> list[Any]: ...",
    'def reload(self) -> "{D}": ...',
    "def summary(self) -> dict[str, Any]: ...",
    "def click(self, selector: str | None = ..., *, timeout: float = ...,",
    '          optional: bool = ...) -> "{D}": ...',
    "def write(self, selector: str, text: str, *, timeout: float = ...,",
    '          optional: bool = ...) -> "{D}": ...',
    "def wait_for(self, selector: str | None = ..., *,",
    '             timeout: float = ...) -> "{D}": ...',
    "def evaluate(self, script: str) -> Any: ...",
    'def screenshot(self, selector: str | None = ...) -> "{D}": ...',
    "@overload",
    'def render(self, format: Literal["elements"]) -> "list[Element]": ...',
    "@overload",
    'def render(self, format: Literal["links"]) -> "{CR}": ...',
    "@overload",
    "def render(self, format: str, **options: Any) -> str: ...",
]

_COLLECTION_LIFT = [
    "def select(self, selector: str, *, index: int = ...,",
    '           error: Any = ...) -> "Collection[Document]": ...',
    "def select_all(self, selector: str, *, limit: int | None = ...,",
    '               offset: int = ...) -> "Collection[Document]": ...',
    'def attr(self, name: str, *, error: Any = ...) -> "Collection[Field[str]]": ...',
    'def text(self) -> "Collection[Field[str]]": ...',
    'def render(self, format: str, **options: Any) -> "Collection[Field[Any]]": ...',
]

EAGER = {
    "D": "Document",
    "R": "Reference",
    "FS": "Field[str]",
    "FB": "Field[bool]",
    "CD": "Collection[Document]",
    "CR": "Collection[Reference]",
}


def _expand(lines: list[str], tier: dict[str, str]) -> list[str]:
    out = []
    for line in lines:
        for key, val in tier.items():
            line = line.replace("{" + key + "}", val)
        out.append("        " + line if line else line)
    return out


_LAZY_TIER = '''# fmt: off
class LazyField(Lazy["Field[S]"], Generic[S]):
    def get(self, default: Any = ...) -> S: ...
    def is_ok(self) -> "LazyField[bool]": ...
    def is_empty(self) -> "LazyField[bool]": ...
    def __eq__(self, o: Any) -> "LazyField[bool]": ...  # type: ignore[override]
    def __ne__(self, o: Any) -> "LazyField[bool]": ...  # type: ignore[override]
    def __and__(self, o: Any) -> "LazyField[bool]": ...
    def __or__(self, o: Any) -> "LazyField[bool]": ...
    def __invert__(self) -> "LazyField[bool]": ...
    def collect(self, context: Any = ...) -> "Field[S]": ...


class LazyReference(Lazy["Reference"]):
    url: "LazyField[str]"
    def resolve(self, *, browser: bool = ..., optional: bool = ..., error: Any = ...) -> "LazyDocument": ...
    def with_params(self, **params: str) -> "LazyReference": ...
    def replace(self, **fields: Any) -> "LazyReference": ...
    def join(self, href: str) -> "LazyReference": ...
    def collect(self, context: Any = ...) -> "Reference": ...


class LazyDocument(Lazy["Document"]):
    def select(self, selector: str, *, index: int = ..., error: Any = ...) -> "LazyDocument": ...
    def select_all(self, selector: str, *, limit: int | None = ..., offset: int = ...) -> "LazyCollection[LazyDocument]": ...
    @overload
    def attr(self, name: Literal["href", "src", "action"]) -> "LazyReference": ...  # type: ignore[overload-overlap]
    @overload
    def attr(self, name: str, *, error: Any = ...) -> "LazyField[str]": ...
    def field(self, name: str) -> "LazyField[Any]": ...
    def reference(self, name: str) -> "LazyReference": ...
    def is_ok(self) -> "LazyField[bool]": ...
    def is_empty(self) -> "LazyField[bool]": ...
    def render(self, format: str, **options: Any) -> "LazyField[Any]": ...
    def collect(self, context: Any = ...) -> "Document": ...


class LazyCollection(Lazy["Collection[T]"], Generic[T]):
    def extract(self, **exprs: Any) -> "LazyCollection[T]": ...
    def filter(self, *predicates: Any) -> "LazyCollection[T]": ...
    def limit(self, n: int) -> "LazyCollection[T]": ...
    def documents(self, column: str) -> "LazyCollection[LazyDocument]": ...
    def project(self) -> "list[dict[str, Any]]": ...
    def collect(self, context: Any = ...) -> "Collection[T]": ...
# fmt: on'''


def _body(region: str) -> str:
    if region == "lazy-tier":
        return _LAZY_TIER
    if region == "Reference eager surface":
        # fields that are ops (url/ok props, actions unused in stub) are skipped
        rows = _fields(ReferenceCore, {"actions"}) + _expand(_REFERENCE_OPS, EAGER)
    elif region == "Document eager surface":
        rows = (
            _fields(DocumentCore, {"final_url", "error", "text", "title"})
            + _expand(_DOCUMENT_OPS, EAGER)
            + [
                "        @property",
                "        def final_url(self) -> str | None: ...",
                "        @property",
                "        def error(self) -> Any: ...",
            ]
        )
    elif region == "collection element-op lifting":
        rows = _expand(_COLLECTION_LIFT, EAGER)
    else:
        raise KeyError(region)
    # fmt guards keep black off the generated block, so its exact text (and
    # thus --check) stays stable regardless of formatting runs.
    return "        # fmt: off\n" + "\n".join(rows) + "\n        # fmt: on"


REGIONS = [
    (SURFACES, "Reference eager surface"),
    (SURFACES, "Document eager surface"),
    (COLLECTION, "collection element-op lifting"),
    (MODELS, "lazy-tier"),
]


def main(check: bool) -> int:
    stale: list[str] = []
    for path, region in REGIONS:
        text = path.read_text()
        start = f"# >>> generated: {region} <<<\n"
        lo = text.index(start) + len(start)
        # end marker, indent-agnostic: back up to the start of its line
        e = text.index(">>> end generated <<<", lo)
        hi = text.rindex("\n", lo, e) + 1
        body = _body(region) + "\n"
        if text[lo:hi] != body:
            if check:
                stale.append(f"{path.name}: {region}")
            else:
                path.write_text(text[:lo] + body + text[hi:])
    if check and stale:
        print("stale generated stubs:\n  " + "\n  ".join(stale))
        return 1
    print("stubs up to date" if check else "stubs regenerated")
    return 0


if __name__ == "__main__":
    sys.exit(main(check="--check" in sys.argv))
