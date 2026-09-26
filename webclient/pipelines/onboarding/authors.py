"""onboarding.authors -- the query-authoring ENGINE seam.

``write_query`` owns the test / repair / recency-retry / artifact orchestration; an Author owns only
"produce the next candidate query expressions". The ``text`` engine prompts the model for query CODE
and parses it; the ``index`` engine drives the element-index query loop (the model picks record/field
NUMBERS and ``build_query`` assembles the selectors) with the text author as its fallback."""

import abc
from typing import Any

from .common import LLM, log
from .llm import _ask_json
from .artifacts import Brief
from .query_build import _parse_queries


class AuthoringError(Exception):
    """The author could not produce a valid query this turn (e.g. the model's reply didn't parse).
    :func:`write_query` catches it and retries with feedback -- distinct from an ``LlmError`` (a
    transport/API failure, which aborts)."""

class Author(abc.ABC):
    """The query-authoring ENGINE seam. ``write_query`` owns the test / repair / recency-retry /
    artifact orchestration; the author owns only "produce the next candidate query expressions".
    This is the ONE boundary Phase 6 swaps -- from :class:`_TextAuthor` (prompt the model for
    query CODE and parse it) to an index-loop author (the model picks record/field indexes and
    ``llm.query_agent.build_query`` assembles the query). The orchestration is engine-agnostic."""

    @abc.abstractmethod
    def author(self, follow_up: "str | None" = None) -> "list[Any]":
        """The next candidate as 1..N query exprs (one per section). ``follow_up`` is the feedback
        from the last rejected attempt (``None`` on the first). Raises :class:`AuthoringError` when
        it cannot produce a valid query, so ``write_query`` retries with feedback."""

class _TextAuthor(Author):
    """Authors by prompting the model for query CODE and parsing it (the current engine). The PAGE
    (guide + skeleton + brief) is the OPENING message; each retry sends only the short feedback. If
    the model keeps a conversation (an :class:`LlmClient` exposes ``.conversation()``), the page
    stays in context and is re-read from cache instead of re-submitted every attempt; a plain
    ``Callable[[str], str]`` has no memory, so the page is re-sent each turn (the fallback)."""

    def __init__(self, llm: LLM, opening: str) -> None:
        conv = getattr(llm, "conversation", None)
        self._chat: Any = conv() if callable(conv) else None
        self._llm = llm
        self._opening = opening
        self._opened = False

    def _send(self, follow_up: "str | None" = None) -> str:
        """One authoring turn -> the model's raw reply (the opening on the first turn, then only
        the feedback for a conversational model, else the opening re-sent with the feedback)."""
        if self._chat is not None:  # stateful: the page once, then just the follow-up
            msg = self._opening if not self._opened else (follow_up or "Try again.")
            self._opened = True
            return str(self._chat.send(msg))
        # stateless callable: no memory -> the page must ride along every turn
        return self._llm(self._opening if not follow_up else f"{self._opening}\n\n{follow_up}")

    def author(self, follow_up: "str | None" = None) -> "list[Any]":
        """Send the turn and parse the reply into 1..N section queries; an unparsable reply is an
        :class:`AuthoringError` so ``write_query`` retries with the "reply with ONLY query code"
        feedback."""
        reply = self._send(follow_up)
        try:
            return _parse_queries(reply)  # 1 wq.doc chain, or one per section (split on ---)
        except Exception as exc:  # noqa: BLE001 - unparsable query code -> a retryable authoring miss
            log.debug("      unparseable reply: %.200r", reply.strip())
            raise AuthoringError(str(exc)) from exc

def _query_loop_prompt(brief: Brief, obs: Any, follow_up: "str | None") -> str:
    """The per-round prompt for the index-based query policy: the numbered RECORD options (the
    repeated structures) and FIELD options (the chosen record's leaves) with the brief's required
    fields, the query + sample so far, and any feedback -- asking for a JSON pick BY NUMBER (never
    a selector)."""
    records = "\n".join(f"  R{e.index}: {e.name} (repeats {e.repeats})" for e in obs.records) or "  (none)"
    fields = "\n".join(f'  F{e.index}: {e.role} "{e.name}"' for e in obs.fields) or "  (none)"
    want = ", ".join(brief.fields) or "the dataset's fields"
    sofar = f"\n\nQuery so far:\n  {obs.query}\nSample rows so far:\n  {obs.sample[:3]}" if obs.query else ""
    fb = f"\n\nFeedback to address: {obs.error or follow_up}" if (obs.error or follow_up) else ""
    return (
        "You are building a data-extraction query by PICKING NUMBERS -- never write a CSS "
        "selector. Choose the REPEATED RECORD that holds the dataset, then map each required "
        f"field to one of that record's FIELDS.\n\nRequired fields: {want}\n\n"
        f"RECORDS (repeated structures -- pick the dataset):\n{records}\n\n"
        f"FIELDS (leaves of the first record -- one per required field):\n{fields}{sofar}{fb}\n\n"
        'Reply with ONLY JSON: {"record": <R-number or null>, "fields": {"<field name>": '
        '<F-number>, ...}, "done": <true|false>}. Set record on the first turn; add fields; set '
        "done=true once the sample has every required field."
    )

def _llm_query_policy(llm: LLM, brief: Brief, follow_up: "str | None") -> Any:
    """An index-query policy backed by ``llm``: each round it prompts with the record/field
    options + sample and parses a JSON ``{record, fields, done}`` into a
    :class:`~webclient.llm.query_agent.QueryDecision` -- the model reasons in NUMBERS, the loop
    builds the selectors."""
    from ...llm.query_agent import QueryDecision

    def policy(obs: Any) -> Any:
        data = _ask_json(llm, _query_loop_prompt(brief, obs, follow_up)) or {}
        rec = data.get("record")
        cols = {
            str(k): int(v) for k, v in (data.get("fields") or {}).items()
            if isinstance(v, (int, float)) and not isinstance(v, bool)
        }
        return QueryDecision(
            record=int(rec) if isinstance(rec, (int, float)) and not isinstance(rec, bool) else None,
            fields=cols, done=bool(data.get("done")),
        )

    return policy

class _LoopAuthor(Author):
    """Authors by DRIVING THE INDEX QUERY LOOP (Phase 6): the model picks record/field NUMBERS
    from ``doc``'s element index and ``llm.query_agent.build_query`` assembles the durable
    ``select_all(record).extract(fields).project()`` query -- the model never writes a selector.

    Record detection is a HINT, not a requirement: the index engine is a best-effort FAST-PATH
    tried ONCE. It hands off to ``fallback`` (the text author) when it can't help -- immediately
    when ``record_options`` surfaces nothing (no wasted LLM calls on a doomed pick), or on the
    next ``write_query`` retry if its query was rejected. So the index path can only ADD queries,
    never block one: a page its detector can't classify degrades to the proven text engine."""

    def __init__(self, llm: LLM, doc: Any, brief: Brief, fallback: "Author | None" = None) -> None:
        self._llm = llm
        self._doc = doc
        self._brief = brief
        self._fallback = fallback
        self._tried = False

    def author(self, follow_up: "str | None" = None) -> "list[Any]":
        from ...core.document.element_index import record_options
        from ...core.document.html import tree
        from ...llm.query_agent import build_query
        from ...query.expr import from_blob

        # a retry means the index engine's one shot was rejected -> hand off to text for recovery.
        if self._tried and self._fallback is not None:
            return self._fallback.author(follow_up)
        self._tried = True

        # no record hint at all -> don't burn LLM calls on a doomed index loop; go straight to text.
        has_hint = bool(record_options(tree(self._doc))) if getattr(self._doc, "ok", True) else False
        if has_hint:
            run = build_query(self._doc, _llm_query_policy(self._llm, self._brief, follow_up))
            if run.blob and run.row_count > 0:
                return [from_blob(run.blob)]
        if self._fallback is not None:
            log.info("    index author had no usable query (detection weak) — using the text author")
            return self._fallback.author(follow_up)
        raise AuthoringError("the index query loop produced no query and no fallback was set")

def _make_author(
    llm: LLM, prompt: str, doc: Any, brief: Brief, *, engine: str = "text"
) -> Author:
    """Build the query author for a ``write_query`` run -- THE single Phase-6 seam. ``engine``
    selects it: ``"text"`` (the default) returns the :class:`_TextAuthor` (prompt the model for
    query code); ``"index"`` returns the :class:`_LoopAuthor` -- the index fast-path with the text
    author as its FALLBACK, so record detection is a hint that can only help. ``write_query``'s
    test / repair / retry / artifact orchestration is the same either way."""
    if engine == "index":
        return _LoopAuthor(llm, doc, brief, fallback=_TextAuthor(llm, prompt))
    return _TextAuthor(llm, prompt)
