"""Bundled LLM skills, shipped as package data under ``webclient/skills/``.

These are reference guides an agent can pull at runtime (also exposed as an MCP
tool). They are pure text -- no client, no fetch -- so they load with nothing but
the base install.
"""

from __future__ import annotations

from importlib.resources import files
from typing import Any

#: the query ops an authored query uses, in a teaching order. The reference text for
#: each is pulled LIVE from its docstring (so the guide can never drift from the
#: surface); anything here that no longer exists is simply skipped. ``text_content`` is
#: deliberately omitted -- ``attr("text")`` is the single way to read text, so the
#: query author is never faced with two ways to do the same thing.
_QUERY_OPS: tuple[str, ...] = (
    "select", "select_all", "attr", "regex", "regex_all", "html", "links",
    "skeleton", "is_ok", "is_empty",
)
#: the collection-shaping ops (hand-written on ``Collection``).
_COLLECTION_OPS: tuple[str, ...] = ("extract", "filter", "project", "limit")

_MARKER = "<!-- OP-REFERENCE -->"


def _skill(name: str) -> str:
    """The text of a bundled skill (``webclient/skills/<name>.md``)."""
    return files("webclient").joinpath(f"skills/{name}.md").read_text(encoding="utf-8")


def _first_sentence(doc: str | None) -> str:
    """The first sentence of a docstring, whitespace-collapsed -- the one-line gloss."""
    if not doc:
        return ""
    text = " ".join(doc.split())
    cut = text.find(". ")
    return (text[: cut + 1] if cut != -1 else text).strip()


def _op_docs(ops: "tuple[str, ...]", providers: "list[Any]") -> "dict[str, str]":
    """Map each op in ``ops`` to its live docstring gloss, taken from the first
    ``providers`` class that defines it (a backing class, or ``Collection``)."""
    out: dict[str, str] = {}
    for op in ops:
        for provider in providers:
            fn = getattr(provider, op, None)
            gloss = _first_sentence(getattr(fn, "__doc__", None))
            if gloss:
                out[op] = gloss
                break
    return out


def _lazy_op_reference() -> str:
    """The op reference, generated from the live document + collection surfaces and
    their docstrings -- so it always matches what the ops actually do."""
    from .collection import Collection
    from .core.document import Document

    backings = [type(b) for b in Document.BACKINGS]
    doc_docs = _op_docs(_QUERY_OPS, backings)
    coll_docs = _op_docs(_COLLECTION_OPS, [Collection])
    lines = ["On the page or a selected element (a `wq.doc` chain):"]
    lines += [f"- `.{op}` — {doc_docs[op]}" for op in _QUERY_OPS if op in doc_docs]
    lines.append("\nOn a `select_all` set of records (shaping rows):")
    lines += [f"- `.{op}` — {coll_docs[op]}" for op in _COLLECTION_OPS if op in coll_docs]
    return "\n".join(lines)


def lazy_query_guide() -> str:
    """The web-query skill an LLM uses to turn a target schema + a page skeleton into a
    query (``wq.doc`` root, select/extract/filter/project/regex, durable CSS selectors,
    worked examples). The op reference is generated live from the document/collection
    surfaces' docstrings, so it never drifts. Query syntax only -- fetching/resolving
    are the caller's job."""
    body = _skill("lazy-queries")
    if _MARKER in body:
        body = body.replace(_MARKER, _lazy_op_reference())
    return body


__all__ = ["lazy_query_guide"]
