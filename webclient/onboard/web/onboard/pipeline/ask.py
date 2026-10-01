"""``ask(ctx, STAGE, **args)`` -- the ONE way a stage talks to the model.

``pipeline/prompts/<stage>.md`` is a tiny :class:`string.Template`: a line of goal, the arguments
the stage needs, the reply shape. ``args`` are brief / state values. The call is metered: its
cost (from the client's own accounting) is charged to the stage on the state. :func:`ask_json`
parses a typed reply and retries ONCE with the validation error when the reply is not the shape
asked for -- the full reply is kept in the error so a human can see why.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from functools import lru_cache
from importlib.resources import files
from string import Template
from typing import Protocol, TypeVar, runtime_checkable

from pydantic import BaseModel, ValidationError
from web.fetch import emit
from web.resolve import Resolver

from ..compile import clean_reply
from ..llm import Llm, ReasonEvent
from ..search import Search
from .state import Onboarding

T = TypeVar("T", bound=BaseModel)

#: the character budget of any page-derived prompt input (~700 tokens): a prompt stays tiny.
PROMPT_INPUT_CHARS = 2_800


@runtime_checkable
class Metered(Protocol):
    """A model client that reports its own running spend (the Anthropic client and the shim do)."""

    spent_usd: float
    calls: int


@dataclass
class Context:
    """The live objects a run needs (never in the state): the resolver, the model, the search,
    and a per-run document cache (a fetched page the next stage reuses; refetched on resume)."""

    resolver: Resolver
    llm: Llm
    search: Search
    docs: "dict[str, object]" = field(default_factory=dict)


class ReplyError(ValueError):
    """The model's reply was not the shape the stage asked for (the full reply is in the message)."""


@lru_cache(maxsize=None)
def _template(stage: str) -> Template:
    text = files(__name__.rsplit(".", 1)[0] + ".prompts").joinpath(f"{stage}.md").read_text()
    return Template(text.rstrip("\n"))


def render(stage: str, **args: str) -> str:
    """The stage's prompt rendered (every ``$arg`` must be supplied -- a typo fails loudly)."""
    return _template(stage).substitute(args)


def _spent(llm: Llm) -> "tuple[int, float]":
    return (llm.calls, llm.spent_usd) if isinstance(llm, Metered) else (0, 0.0)


async def ask(
    ctx: Context, state: Onboarding, stage: str, *, prompt: "str | None" = None, **args: str
) -> str:
    """One metered model call charged to ``stage``, over the ``prompt`` template (``stage`` by
    default -- a stage reusing another's prompt names it); the reply text."""
    calls0, usd0 = _spent(ctx.llm)
    reply = await ctx.llm.complete(render(prompt or stage, **args))
    calls1, usd1 = _spent(ctx.llm)
    state.charge(stage, calls1 - calls0, usd1 - usd0)
    return reply


async def ask_json(ctx: Context, state: Onboarding, stage: str, model: "type[T]", **args: str) -> T:
    """:func:`ask`, with the reply parsed as JSON into ``model``. An unparseable / wrong-shaped
    reply is retried once with the error quoted; the second failure raises :class:`ReplyError`."""
    reply = await ask(ctx, state, stage, **args)
    try:
        return _parse(reply, model)
    except ReplyError as exc:
        emit(ReasonEvent(stage=stage, text=f"reply not the asked shape — retrying once: {exc}"))
        again = await ask(
            ctx,
            state,
            stage,
            **{
                **args,
                "note": f"\nYour previous reply was not valid: {exc}. Reply with the JSON only.",
            },
        )
        return _parse(again, model)


def _parse(reply: str, model: "type[T]") -> T:
    text = clean_reply(reply)
    start = min((i for i in (text.find("{"), text.find("[")) if i != -1), default=-1)
    if start == -1:
        raise ReplyError(f"no JSON in the reply: {' '.join(reply.split())[:600]!r}")
    end = max(text.rfind("}"), text.rfind("]"))
    try:
        value = json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        raise ReplyError(f"{exc.msg}: {' '.join(reply.split())[:600]!r}") from exc
    if not isinstance(value, dict):  # a bare list -> the model's single list field
        single = [n for n, f in model.model_fields.items() if f.annotation is not None]
        value = {single[0]: value} if single else value
    try:
        return model.model_validate(value)
    except ValidationError as exc:
        raise ReplyError(
            f"{exc.errors()[0].get('msg')}: {' '.join(reply.split())[:600]!r}"
        ) from exc


__all__ = ["Context", "Metered", "PROMPT_INPUT_CHARS", "ReplyError", "ask", "ask_json", "render"]
