"""The ``web`` CLI -- Locate and Author, driven entirely by a BRIEF.

    web locate <brief> [entity]     find WHERE the dataset is  -> a Reference
    web author <brief> [entity]     write the wq query that extracts it -> a query

The brief (a packaged name -- ``news`` / ``products`` / ``people`` -- or a markdown file with YAML
frontmatter) is the single source of truth for WHAT to get and HOW: goal + schema (shared),
find-slice (seeds/candidates/search/…) for Locate, shape-slice (selectors/optional/hints/…) for
Author. The optional ``entity`` targets a specific instance -- a company/site -- by folding into the
brief's search qualifier (``web locate news BBC`` searches for BBC's news).

Chaining is two easy ways:
  * run separately -- ``web locate news BBC`` caches the located Reference; ``web author news BBC``
    then picks it up automatically (no piping). Inspect the located source in between.
  * pipe -- ``web locate news BBC | web author news --ref -`` (stdout is the Reference JSON).

Each subcommand prints its serialised ARTIFACT to stdout (the Reference JSON / the wq blob) and the
human-readable REASONING + live progress to stderr. The code interface is :func:`web.onboard.locate`
+ :func:`web.onboard.author`. The model comes from ``--model`` / ``ANTHROPIC_API_KEY``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
from importlib.resources import files
from pathlib import Path
from typing import Protocol, Sequence, runtime_checkable

from web.crawl import CrawlEvent, FrontierMiddleware
from web.fetch import Event, EventBus, FetchEvent
from web.fetch import Profile as FetchProfile
from web.fetch import WebException, using

from web.resolve import EscalationPolicy, ResolveEvent, Resolver, profiles

from .author import AuthorEvent, build_query
from .authoring import author_agent
from .compile import QueryError
from .frontier import llm_frontier
from .llm import AnthropicLlm, Llm, LlmEvent, Pricing, RateLimit, ReasonEvent, Usage
from .locate import locate
from .models import Brief, Reference
from .review import review
from .search import DdgSearch
from .shim import ClaudeShim


@runtime_checkable
class _Metered(Protocol):
    """A client that meters its own spend (the Anthropic client does; the shim does not)."""

    calls: int
    spent_usd: float
    usage: Usage


def _err(*lines: str) -> None:
    """Human-readable reasoning + live progress go to stderr (flushed, so they stream immediately
    even when stdout is piped), keeping stdout a clean, pipeable artifact."""
    for line in lines:
        print(line, file=sys.stderr, flush=True)


def _short(url: str, n: int = 78) -> str:
    return url if len(url) <= n else url[: n - 1] + "…"


class _Progress:
    """A live bus subscriber that streams what the run is doing to stderr, so a long search/crawl/
    author is visibly working (not hung). Installs itself as the ambient event bus for the scope;
    prints each crawled page + each resolve-policy step (retries/escalations), and -- with
    ``verbose`` -- every fetch. Tracks pages + elapsed for a closing summary."""

    def __init__(self, verbose: bool) -> None:
        self._verbose = verbose
        self._bus = EventBus()
        self.pages = 0
        self.llm_calls = 0
        self.llm_spent = 0.0
        self._start = 0.0

    def _on(self, event: Event) -> None:
        if isinstance(event, CrawlEvent):
            self.pages = event.fetched
            mark = "ok " if event.ok else "!! "  # !! = a bad status / transport failure
            flags = f"  flags={','.join(event.flags)}" if event.flags else ""
            _err(
                f"  · [{event.fetched:>2}] {event.status or '---'} {mark}"
                f"{_short(event.url, 62)}{flags}"
            )
        elif isinstance(event, LlmEvent):  # cost AS IT GOES -- one line per model call
            self.llm_calls = event.calls
            self.llm_spent = event.spent_usd
            _err(
                f"  · llm [{event.model}] call {event.calls}: ${event.cost_usd:.4f}"
                f"  (running ${event.spent_usd:.4f})"
            )
        elif isinstance(event, ReasonEvent):  # WHY a choice was made
            subj = f"{_short(event.subject, 60)} — " if event.subject else ""
            _err(f"  ⋯ {event.stage}: {subj}{event.text}")
        elif isinstance(event, AuthorEvent):  # the authoring stages
            if event.phase == "sample":
                fl = ", ".join(event.flags) or "—"
                _err(f"  · sample: {event.kind}, {event.lines} skeleton line(s), flags: {fl}")
            elif event.phase == "reply":
                _err(f"  · model wrote: {_short(' '.join(event.reply.split()), 100)}")
            elif event.phase == "parsed":
                _err("  · query parsed + rerooted at the source")
        elif isinstance(event, ResolveEvent):
            _err(f"  · {event.phase}: {_short(event.url)}")
        elif isinstance(event, FetchEvent) and self._verbose:
            _err(f"  · fetch {event.status} ({event.elapsed:.2f}s): {_short(event.url)}")

    def __enter__(self) -> "_Progress":
        self._start = time.monotonic()
        self._bus.subscribe("", self._on)
        self._using = using(self._bus)
        self._using.__enter__()
        return self

    def __exit__(self, *exc: object) -> None:
        self._using.__exit__(*exc)

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self._start


# -- brief + entity + reference cache -------------------------------------------------------------


def _packaged_briefs() -> "list[str]":
    """The names of the briefs bundled with the package (``web/onboard/briefs/*.md``)."""
    try:
        root = files("web.onboard").joinpath("briefs")
        return sorted(p.name[:-3] for p in root.iterdir() if p.name.endswith(".md"))
    except Exception:
        return []


def _load_brief(arg: str) -> Brief:
    """Resolve the ``<brief>`` positional: a path to a markdown file, else a packaged brief by name
    (``web/onboard/briefs/<name>.md``; ``-``/``_`` interchangeable). See :func:`_packaged_briefs`.
    """
    if Path(arg).is_file():
        return Brief.load(arg)
    for name in {arg, arg.replace("-", "_"), arg.replace("_", "-")}:
        res = files("web.onboard").joinpath(f"briefs/{name}.md")
        if res.is_file():
            return Brief.from_markdown(res.read_text(encoding="utf-8"))
    raise SystemExit(
        f"no brief {arg!r}: not a file, and no packaged briefs/{arg}.md. "
        f"Available packaged briefs: {', '.join(_packaged_briefs()) or '(none)'}"
    )


def _apply_entity(brief: Brief, entity: "str | None") -> Brief:
    """Fold a target ENTITY (a company/site) into the brief's search qualifier, so ``web locate news
    BBC`` searches for BBC's news. A ``{entity}`` placeholder in the brief's ``search`` is
    substituted; otherwise the entity is prepended to the search qualifier (else the goal). No-op
    without an entity. (An explicit source -- seeds/candidates/start_url -- still makes search
    irrelevant, so the entity only matters when the brief searches.)"""
    if not entity:
        return brief
    if "{entity}" in brief.search:
        search = brief.search.replace("{entity}", entity)
    else:
        search = f"{entity} {brief.search or brief.goal}".strip()
    return brief.model_copy(update={"search": search})


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-") or "brief"


def _cache_path(brief: Brief, brief_arg: str, entity: "str | None") -> Path:
    """Where a located Reference is cached, keyed by brief + entity -- so ``author`` picks up what
    ``locate`` found. Under ``$XDG_CACHE_HOME`` (else ``~/.cache``)/``web-onboard``."""
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    key = _slug(brief.name or Path(brief_arg).stem)
    name = key + (f"__{_slug(entity)}" if entity else "")
    return Path(base) / "web-onboard" / f"{name}.json"


def _reference_from_ref(ref: str) -> Reference:
    """A Reference from ``--ref``: ``-`` reads locate's JSON from stdin (the pipe form), else a file."""
    if ref == "-":
        return Reference.model_validate_json(sys.stdin.read())
    with open(ref, encoding="utf-8") as fh:
        return Reference.model_validate_json(fh.read())


# -- transport / resolver -------------------------------------------------------------------------


def _resolver(
    profile_name: str, proxy: "str | None", browser_path: "str | None" = None
) -> Resolver:
    """The resolver for the run from a named profile (+ optional proxy). ``basic`` is HTTP;
    ``basic_browser`` escalates to a browser on a block; ``full_browser`` always renders.
    ``browser_path`` pins the browser BINARY that any browser tier launches."""
    if proxy:
        factory = {
            "basic": profiles.proxy,
            "basic_browser": profiles.proxy_browser,
            "full_browser": profiles.proxy_full_browser,
        }[profile_name]
        profile = factory(proxy)
    else:
        got = profiles.get(profile_name)
        if got is None:
            return Resolver()
        profile = got
    if browser_path and profile.escalation:  # pin the binary on every browser tier
        tiers = tuple(
            (
                t.with_(executable_path=browser_path)
                if isinstance(t, FetchProfile) and t.browser
                else t
            )
            for t in profile.escalation.tiers
        )
        profile = profile.with_(escalation=EscalationPolicy(tiers=tiers, on=profile.escalation.on))
    return Resolver(profile=profile)


def _transport_args(sub: argparse.ArgumentParser) -> None:
    """Operational options shared by both subcommands (transport + progress) -- NOT brief content."""
    sub.add_argument(
        "brief", help="a packaged brief name (news/products/people) or a markdown file"
    )
    sub.add_argument(
        "entity",
        nargs="?",
        default=None,
        help="optional: a specific target (e.g. a company) -- folds into the brief's search",
    )
    sub.add_argument(
        "--profile",
        default="basic_browser",
        choices=("basic", "basic_browser", "full_browser"),
        help="resolve profile (default basic_browser: HTTP first, escalate to a browser on a block "
        "/403 -- robust; `basic` is HTTP-only, `full_browser` always renders)",
    )
    sub.add_argument(
        "--full-browser",
        dest="profile",
        action="store_const",
        const="full_browser",
        help="shorthand for --profile full_browser",
    )
    sub.add_argument(
        "--proxy", default=None, help="proxy URL for all traffic (http://[user:pass@]host:port)"
    )
    sub.add_argument(
        "--browser-path",
        default=None,
        metavar="EXE",
        help="an explicit browser binary (driver/executable) for any browser tier to launch",
    )
    sub.add_argument(
        "--no-cache", action="store_true", help="do not read/write the located-reference cache"
    )
    sub.add_argument(
        "--shim",
        action="store_true",
        help="use a REAL model via the local `claude -p` CLI (no API key): drives the LLM crawl "
        "frontier in `locate`, and writes the query in `author` (--model is a CLI alias, e.g. haiku)",
    )
    sub.add_argument("--model", default=None, help="LLM model id / CLI alias (else the default)")
    sub.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="stream every fetch too (default streams crawled pages + retries/escalations)",
    )


# -- web locate -----------------------------------------------------------------------------------


def _explain_reference(ref: Reference) -> None:
    """The reasoning behind the located source: what it is, the API preference, the repeating-row
    hint, the pager, and the flags + signal evidence that decided it (all to stderr)."""
    _err("", f"  source:    {ref.url}")
    if ref.api_endpoint:
        _err(
            f"  data-API:  {ref.api_endpoint}  ← live XHR behind the page (query this, not the DOM)"
        )
    elif ref.page_url and ref.page_url != ref.url:
        _err(f"  page:      {ref.page_url}")
    _err(
        f"  kind:      {ref.kind}" + ("   (needs a browser to render)" if ref.needs_browser else "")
    )
    if ref.profile:
        _err(f"  transport: {ref.profile}   (the profile that worked — baked into the query)")
    if ref.record_selector:
        _err(f"  records:   {ref.record_selector}   (the repeating-row selector to extract)")
    if ref.pagination:
        _err(f"  pager:     {ref.pagination}   (the pipeline follows it)")
    if ref.assessment:  # each conclusion, its confidence + meaning, and the evidence that fired it
        _err("  flags:")
        for f in ref.assessment:
            _err(f"    {f.name} ({f.confidence:.2f}) — {f.description}")
            for s in f.signals:
                extra = f" {s.detail}" if s.detail else ""
                _err(f"        · {s.name} ({s.confidence:.2f}){extra}")
    elif ref.flags:
        _err("  flags:     " + ", ".join(ref.flags))
    reason = ref.detail.get("reason")
    if reason:
        _err(f"  chosen:    {reason}")
    score = ref.detail.get("score")
    if score is not None:
        _err(f"  score:     {score}   (dataset-likeness × scrapability)")


def _entity_arg(entity: "str | None") -> str:
    """The entity as it would be retyped on the command line (quoted if it has spaces)."""
    return "" if not entity else (f'"{entity}"' if " " in entity else entity)


async def _locate(args: argparse.Namespace) -> int:
    brief = _apply_entity(_load_brief(args.brief), args.entity)
    resolver = _resolver(args.profile, args.proxy, args.browser_path)
    # --shim drives the LLM stages with a real model (claude -p): the CRAWL FRONTIER (each round it
    # picks which pending edges to expand toward the dataset) AND the FINAL REVIEW (it judges each
    # candidate best-first and must confirm the page holds the requested dataset for the entity).
    frontier: "tuple[FrontierMiddleware, ...]" = ()
    review: "ClaudeShim | None" = None
    if args.shim:
        review = ClaudeShim(model=args.model) if args.model else ClaudeShim()
        frontier = (
            llm_frontier(
                review,
                brief.goal,
                entity=args.entity or "",
                fields=brief.fields,
                look=brief.look,
                ignore=brief.ignore,
            ),
        )
    seeded = bool(brief.seeds or brief.candidates or brief.start_url)
    tag = f"{brief.name or args.brief}" + (f" · {args.entity}" if args.entity else "")
    _err(
        f"locating [{tag}]: "
        + (
            "crawling the brief's sources"
            if seeded
            else f"searching {brief.search or brief.goal!r}"
        )
        + (" · LLM frontier" if args.shim else "")
        + f" then evaluating candidates (max {brief.max_pages} pages)…"
    )
    try:
        with _Progress(args.verbose) as prog:
            reference = await locate(
                brief,
                resolver=resolver,
                search=DdgSearch(k=args.search_k),
                frontier=frontier,
                entity=args.entity or "",
                review=review,
            )
    except WebException as exc:
        _err(f"locate failed: {exc}")
        return 1
    finally:
        await resolver.aclose()
    cost = (
        f"LLM (frontier + review): ${prog.llm_spent:.4f} over {prog.llm_calls} call(s)"
        if prog.llm_calls
        else "deterministic — no LLM cost"
    )
    _err(f"  evaluated {prog.pages or '?'} page(s) in {prog.elapsed:.1f}s ({cost})")
    if reference is None:
        _err(
            "no source passed review (none held the dataset, or every candidate was off-entity / a "
            "third party); add seeds/candidates/start_url to the brief, refine the entity, "
            "or widen the search."
        )
        return 1
    _explain_reference(reference)  # reasoning -> stderr
    if not args.no_cache:
        path = _cache_path(brief, args.brief, args.entity)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(reference.model_dump_json(), encoding="utf-8")
        nxt = f"web author {args.brief}" + (f" {_entity_arg(args.entity)}" if args.entity else "")
        _err(f"  cached →   {path}", f"  next:      {nxt}   (authors over this reference)")
    print(reference.model_dump_json())  # the serialised Reference -> stdout (also pipeable)
    return 0


# -- web author -----------------------------------------------------------------------------------


def _explain_query(
    reference: Reference, brief: Brief, engine: str, describe: str, notes: "list[str]"
) -> None:
    """The reasoning behind the authored query: how it was written, the dataset it targets, the
    schema requested, and the advisory notes -- all to stderr (the blob itself is the stdout artifact).
    """
    _err(
        "",
        f"  source:    {reference.url}  ({reference.kind})",
        f"  engine:    {engine}   (llm = the model wrote it; file_links/file_download = deterministic)",
    )
    if brief.fields:
        _err(
            "  schema:    "
            + ", ".join(
                f
                + (f":{brief.types[f]}" if f in brief.types else "")
                + ("*" if f in brief.optional else "")
                + (f" [{brief.descriptions[f]}]" if f in brief.descriptions else "")
                for f in brief.fields
            )
            + "   (* = optional)"
        )
    if reference.flags:
        _err("  dataset:   " + ", ".join(reference.flags) + "   (flags carried from Locate)")
    _err(f"  query:     {describe}   ← the wq chain")
    for note in notes:
        _err(f"  note:      {note}   (advisory: the static query cannot express this)")


async def _reference_for_author(
    args: argparse.Namespace, brief: Brief, resolver: Resolver, review: "Llm | None"
) -> "Reference | None":
    """The Reference to author over, easiest-first: an explicit ``--ref`` (stdin/file, the pipe
    form); else the cached Reference from a prior ``web locate <brief> [entity]`` (the run-separately
    form); else locate it now (so ``web author`` works standalone). ``brief`` already has the entity
    applied; ``review`` is the LLM used for Locate's final candidate review when we locate here."""
    if args.ref:
        return _reference_from_ref(args.ref)
    path = _cache_path(brief, args.brief, args.entity)
    if not args.no_cache and path.is_file():
        _err(f"  using the located reference: {path}")
        return Reference.model_validate_json(path.read_text(encoding="utf-8"))
    _err(
        "  no located reference cached — locating first (run `web locate` to inspect it separately)…"
    )
    # An entity-aware LLM frontier so the crawl's hostname pruning is the model's call (not a rule).
    frontier: "tuple[FrontierMiddleware, ...]" = ()
    if review is not None:
        frontier = (
            llm_frontier(
                review,
                brief.goal,
                entity=args.entity or "",
                fields=brief.fields,
                look=brief.look,
                ignore=brief.ignore,
            ),
        )
    with _Progress(args.verbose):
        return await locate(
            brief,
            resolver=resolver,
            search=DdgSearch(),
            frontier=frontier,
            entity=args.entity or "",
            review=review,
        )


async def _author(args: argparse.Namespace) -> int:
    brief = _apply_entity(_load_brief(args.brief), args.entity)
    resolver = _resolver(args.profile, args.proxy, args.browser_path)
    llm = _build_llm(args)
    try:
        reference = await _reference_for_author(args, brief, resolver, review=llm)
        if reference is None:
            _err("no source located to author over (nothing scored above zero).")
            return 1
        if args.agent:  # the authoring AGENT LOOP: base -> nested-detail -> ... (extends per brief)
            _err(f"authoring (agent loop): {_short(reference.url)}…")
            with _Progress(args.verbose):
                agent_query, verdict = await author_agent(
                    reference, brief, resolver=resolver, llm=llm
                )
            if agent_query is None:
                _err(f"authoring failed: the agent loop produced no query ({verdict.reason})")
                return 1
            query, engine = agent_query, "agent"
            notes = [f"agent loop: {verdict.rounds} round(s) → {verdict.reason}"]
        else:
            _err(f"authoring: resolving {_short(reference.url)} then asking the model…")
            try:
                with _Progress(args.verbose):
                    query, engine, notes = await build_query(
                        reference, brief, resolver=resolver, llm=llm
                    )
            except QueryError as exc:  # the reply was not a rebuildable wq chain (it was logged)
                _err(f"authoring failed: the model's query did not parse — {exc}")
                return 1
            if args.review:  # run it, review vs the schema, let the model extend/fix the query
                _err(f"reviewing the extracted data ({args.review} round[s])…")
                with _Progress(args.verbose):
                    query, rnotes = await review(
                        query, reference, brief, resolver=resolver, llm=llm, rounds=args.review
                    )
                notes += rnotes
        _explain_query(reference, brief, engine, query.describe(), notes)  # reasoning -> stderr
        print(query.to_blob())  # the serialised query -> stdout
        if isinstance(
            llm, _Metered
        ):  # AnthropicLlm (priced) OR the shim (claude -p's API-equiv cost)
            u = llm.usage
            _err(
                f"  spend:     ${llm.spent_usd:.4f} over {llm.calls} call(s)"
                f"  (tokens in {u.input}, out {u.output}, cache r/w {u.cache_read}/{u.cache_write})"
            )
        if not args.run:
            return 0
        _err("running the query…")
        try:
            with _Progress(args.verbose):
                rows = await query.acollect(resolver=resolver)
        except WebException as exc:  # a runtime extraction failure (e.g. a loud select miss)
            _err(f"running failed: {exc}")
            return 1
        listed = rows if isinstance(rows, list) else [rows]
        _err(f"\n  rows:      {len(listed)}")
        for row in listed[: args.sample]:
            _err(f"    {json.dumps(row, ensure_ascii=False, default=str)}")
        return 0 if listed else 1
    finally:
        await resolver.aclose()
        await llm.aclose()


def _build_llm(args: argparse.Namespace) -> "AnthropicLlm | ClaudeShim":
    """The LLM for the run. ``--shim`` routes through the local ``claude -p`` CLI (a real model, NO
    API key -- ``--model`` is a CLI alias like ``haiku``); otherwise the Anthropic Messages API
    (needs ``ANTHROPIC_API_KEY``), metered by the ``--rate`` limit and per-million-token prices."""
    if args.shim:
        return ClaudeShim(model=args.model) if args.model else ClaudeShim()
    pricing = Pricing(
        input=args.price_input,
        output=args.price_output,
        cache_read=args.price_cache_read,
        cache_write=args.price_cache_write,
    )
    rate = RateLimit(min_interval=args.rate)
    if args.model:
        return AnthropicLlm(model=args.model, rate=rate, pricing=pricing)
    return AnthropicLlm(rate=rate, pricing=pricing)


# -- entry ----------------------------------------------------------------------------------------


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="web",
        description="Locate a dataset and author its query -- driven by a brief.",
        epilog="e.g. web locate news BBC   then   web author news BBC --run",
    )
    subs = parser.add_subparsers(dest="cmd", required=True)

    loc = subs.add_parser("locate", help="find WHERE the dataset is (-> a Reference)")
    _transport_args(loc)  # adds the `brief` + `entity` positionals too
    loc.add_argument(
        "--search-k", type=int, default=6, metavar="N", help="how many search results to seed from"
    )

    aut = subs.add_parser("author", help="write the wq query that extracts the dataset")
    _transport_args(aut)
    aut.add_argument(
        "--ref",
        default=None,
        metavar="FILE",
        help="use this Reference instead of the cache: '-' reads locate's JSON from stdin, else a file",
    )
    aut.add_argument(
        "--rate",
        type=float,
        default=0.0,
        metavar="SECS",
        help="min seconds between LLM calls (a shared/corporate key)",
    )
    aut.add_argument(
        "--price-input",
        type=float,
        default=0.0,
        metavar="USD",
        help="input price ($/million tokens) -- for the spend report",
    )
    aut.add_argument(
        "--price-output", type=float, default=0.0, metavar="USD", help="output price ($/M tokens)"
    )
    aut.add_argument(
        "--price-cache-read",
        type=float,
        default=0.0,
        metavar="USD",
        help="cache-read price ($/M tokens)",
    )
    aut.add_argument(
        "--price-cache-write",
        type=float,
        default=0.0,
        metavar="USD",
        help="cache-write price ($/M tokens)",
    )
    aut.add_argument(
        "--agent",
        action="store_true",
        help="author with the AGENT LOOP: base query, then extend per the brief (e.g. fetch a "
        "sampled record's link and nest a detail extraction) until the schema is satisfied",
    )
    aut.add_argument(
        "--review",
        type=int,
        default=0,
        metavar="N",
        help="review the extracted data against the schema and let the model revise the query "
        "(fix selectors, add detail, follow links) for N rounds (default 0 = off; ignored with --agent)",
    )
    aut.add_argument(
        "--run", action="store_true", help="run the authored query and print a sample of rows"
    )
    aut.add_argument(
        "--sample", type=int, default=5, metavar="N", help="how many rows to print (default 5)"
    )
    return parser


def main(argv: "Sequence[str] | None" = None) -> int:
    args = _parser().parse_args(argv)
    runner = {"locate": _locate, "author": _author}[args.cmd]
    return asyncio.run(runner(args))


if __name__ == "__main__":
    sys.exit(main())
