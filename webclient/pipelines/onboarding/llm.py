"""onboarding.llm -- see the package docstring."""


import abc
import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Callable, Literal, Sequence

from pydantic import BaseModel, model_validator

#: the pipeline's logger. Stages log progress here (seeds, crawl, candidates, the
#: evaluation, the query, spend); a CLI or app sets the level / handler. Each line is
#: also appended to ``OnboardingResult.steps`` for a programmatic trace.

from ...core.crawl import from_picks
from ...core.document.models import Flag
from ...policy import (
    AntiBotPolicy,
    BrowserPolicy,
    ProxyPolicy,
    Resolve,
)
from ...llm.guides import lazy_query_guide
from ...query.expr import from_blob
from ...interface import Reference, WebClient, wq
from ...clients.llm import Budget, BudgetExceeded, LlmClient, LlmError
from ...llm.prompts import render_prompt

from .common import LLM, log
from .artifacts import SchemaField, Brief


# --------------------------------------------------------------------------- #
# LLM plumbing: prompt -> parsed JSON, defensively.
# --------------------------------------------------------------------------- #


def _strip_fences(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[-1]  # drop the opening ``` / ```json line
        if t.rstrip().endswith("```"):
            t = t.rstrip()[:-3]
    return t.strip()


def _json_blob(text: str) -> str:
    """The first balanced JSON object/array in ``text`` (models like to wrap it in prose).
    STRING-AWARE: a brace/bracket inside a JSON string value (``"the } brace"``, or a selector
    like ``[class*="price"]`` in a reason) does not miscount depth. Falls back to the whole
    stripped string."""
    t = _strip_fences(text)
    starts = [i for i in (t.find("{"), t.find("[")) if i != -1]
    if not starts:
        return t
    start = min(starts)
    depth = 0
    in_str = False
    escaped = False
    for i in range(start, len(t)):
        ch = t[i]
        if in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            depth += 1
        elif ch in "}]":
            depth -= 1
            if depth == 0:
                return t[start : i + 1]
    return t[start:]


def _ask_json(llm: LLM, prompt: str, *, retries: int = 1) -> Any:
    """Run ``llm`` and parse a JSON value from its reply. On a decode error, retry --
    handing the model its own bad output + the parser error so it can fix it -- up to
    ``retries`` times. ``None`` if it still can't produce valid JSON."""
    ask = prompt
    for attempt in range(retries + 1):
        try:
            reply = llm(ask)
        except LlmError as exc:  # a bad-request / exhausted-retry API error -- don't crash
            log.warning("LLM call failed: %s -- skipping this step", exc)
            return None
        try:
            return json.loads(_json_blob(reply))
        except (json.JSONDecodeError, ValueError) as exc:
            if attempt == retries:
                log.warning("LLM reply was not valid JSON after %d tr[y|ies]", attempt + 1)
                return None
            ask = (
                f"{prompt}\n\n---\nYour previous reply could not be parsed as JSON: "
                f"{exc}. Here is what you sent:\n{reply[:800]}\n\nReply again with ONLY "
                "valid JSON -- no prose, no code fences."
            )
    return None


def _schema_outline(fields: "list[SchemaField]", indent: int = 0) -> str:
    """A :class:`SchemaField` tree rendered as an indented outline for a prompt --
    ``- name — description`` per field, nested children indented under their parent."""
    lines: list[str] = []
    for f in fields:
        opt = " (optional)" if f.optional else ""
        desc = f" — {f.description}" if f.description else ""
        lines.append("  " * indent + f"- {f.name}{opt}{desc}")
        if f.children:
            lines.append(_schema_outline(f.children, indent + 1))
    return "\n".join(line for line in lines if line)


def _fields_line(brief: Brief) -> str:
    """The brief's hints as an appended block for any prompt: the target schema (a
    nested outline with per-field descriptions, so the author knows the shape AND how
    to fill each field, nesting a sub-extract per branch) plus the natural-language
    ``look`` / ``ignore`` guides. Empty when the brief carries none."""
    parts: list[str] = []
    if brief.fields:
        parts.append(
            "Target schema (nest a sub-extract per branch so the output JSON matches):\n"
            + _schema_outline(brief.schema_tree())
        )
    if brief.look:
        parts.append("Head for pages like: " + "; ".join(brief.look) + ".")
    if brief.ignore:
        parts.append("Skip pages like: " + "; ".join(brief.ignore) + ".")
    return (" " + " ".join(parts)) if parts else ""


#: the flags the pipeline reads to decide how to fetch, resolve and query a source.
_DECISION_FLAGS = (
    "spa", "shadow_dom", "iframe", "anti_bot_triggered", "login_required",
    "pagination", "forms", "buttons", "large_document",
)


def _read_flags(doc: Any) -> "dict[str, Flag]":
    """The pipeline's decision flags for a document -- each read whether present or
    not, so a stage can branch on ``.present`` / ``.remedy`` / ``.value``."""
    return {name: getattr(doc, name)() for name in _DECISION_FLAGS}


