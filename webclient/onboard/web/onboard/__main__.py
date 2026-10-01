"""The ``web`` CLI -- Locate and Author (driven by a BRIEF), plus raw Fetch and Resolve.

    web fetch <url>                 ONE transport fetch -> the body (httpx/curl_cffi or a browser)
    web resolve <url>               resolve through the escalation ladder -> a parsed document
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
from collections.abc import Sequence
from importlib.resources import files
from pathlib import Path
from typing import Protocol, runtime_checkable
from urllib.parse import urlparse

from pydantic import BaseModel, JsonValue
from web.crawl import CrawlEvent
from web.dsl import Query
from web.fetch import Event, EventBus, FetchEvent, Profile, WebException, aclose_default_pool
from web.fetch import fetch as _fetch_one
from web.fetch import profiles as _fp
from web.fetch import using
from web.resolve import ResolveEvent, Resolver

from .config import PIPELINE_MODEL, build_resolver, default_search
from .config import env as _env
from .config import env_flag as _env_flag
from .config import env_float as _env_float
from .entries import query_of
from .llm import AnthropicLlm, LlmEvent, Pricing, RateLimit, ReasonEvent, TraceEvent, Usage
from .pipeline import STAGE_NAMES, Brief, BriefError, Context, Onboarding, packaged_briefs
from .pipeline import run as pipeline_run
from .pipeline.ask import MAX_TOKENS, SYSTEM
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
        self._stage = "search"  # the stage the next LLM call belongs to (from the trace lines)
        self._sticky: set[str] = set()  # hosts whose sticky tier was already reported
        self.by_stage: "dict[str, list[float]]" = {}  # stage -> [spent, calls]
        self._start = 0.0

    def _on(self, event: Event) -> None:
        if isinstance(event, CrawlEvent):
            self.pages = event.fetched
            mark = "ok " if event.ok else "!! "  # !! = a bad status / transport failure
            flags = f"  flags={','.join(event.flags)}" if event.flags else ""
            # show the FULL url -- a truncated one can't be verified/pasted when a page 404s or a wrong
            # candidate wins (the whole point of the log is to SEE which URL was actually fetched).
            _err(f"  · [{event.fetched:>2}] {event.status or '---'} {mark}{event.url}{flags}")
        elif isinstance(event, LlmEvent):  # cost AS IT GOES -- one line per model call
            self.llm_calls = event.calls
            self.llm_spent = event.spent_usd
            tally = self.by_stage.setdefault(self._stage, [0.0, 0])
            tally[0] += event.cost_usd
            tally[1] += 1
            u = event.usage  # show the token breakdown so the cost (usage x price) is verifiable
            _err(
                f"  · llm [{event.model}] call {event.calls}: "
                f"in={u.input} out={u.output} cache_r={u.cache_read} cache_w={u.cache_write} tok "
                f"→ ${event.cost_usd:.4f}  (running ${event.spent_usd:.4f})"
            )
        elif isinstance(event, TraceEvent):  # the model exchange: the reply always, the prompt -v
            if self._verbose:
                _err(f"  ┌ {event.stage} PROMPT ({len(event.prompt)} chars):")
                for line in event.prompt[:3000].splitlines():
                    _err(f"  │ {line}")
            reply = " ".join(event.reply.split())
            _err(
                f"  └ {event.stage} REPLY ({len(event.reply)} chars): {reply[:1500 if self._verbose else 300]}"
            )
        elif isinstance(event, ReasonEvent):  # WHY a choice was made
            if event.stage != "llm":  # a retry notice is not a stage change
                self._stage = event.stage
            subj = f"{event.subject} — " if event.subject else ""  # full URL/subject, not truncated
            _err(f"  ⋯ {event.stage}: {subj}{event.text}")
        elif isinstance(
            event, ResolveEvent
        ):  # transport fallbacks: which tier a fetch escalated to
            d = event.detail
            if event.phase == "escalate":
                remedy = f" ({d['remedy']})" if d.get("remedy") else ""
                _err(f"  · escalate → tier {d.get('tier')}{remedy}: {event.url}")
            elif event.phase == "sticky":  # once per host (every later same-host fetch uses it)
                host = urlparse(event.url).hostname or event.url
                if host not in self._sticky or self._verbose:
                    self._sticky.add(host)
                    _err(
                        f"  · sticky → tier {d.get('tier')} for {host} (the domain needed it once; "
                        "its later fetches start there)"
                    )
            else:
                _err(f"  · {event.phase}: {event.url}")
        elif isinstance(event, FetchEvent) and self._verbose:
            _err(f"  · fetch {event.status} ({event.elapsed:.2f}s): {event.url}")

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


# -- transport / resolver -------------------------------------------------------------------------


def _resolver(
    profile_name: str, proxy: "str | None", browser_path: "str | None" = None
) -> Resolver:
    """The resolver for the run -- delegates to :func:`web.onboard.config.build_resolver` (which also
    reads WEB_PROFILE / WEB_PROXY / WEB_BROWSER_PATH; the CLI already resolved those into its args).
    """
    return build_resolver(profile=profile_name, proxy=proxy, browser_path=browser_path)


def _transport_args(sub: argparse.ArgumentParser) -> None:
    """Operational options shared by both subcommands (transport + progress) -- NOT brief content.
    Every option here defaults from an env var (WEB_PROFILE / WEB_PROXY / WEB_BROWSER_PATH /
    WEB_SHIM / WEB_LLM_MODEL), so the CLI is fully env-configurable; a flag overrides the env."""
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
        default=_env("WEB_PROFILE", "basic_browser"),
        choices=("basic", "basic_browser", "full_browser"),
        help="resolve profile (default basic_browser: HTTP first, escalate to a browser on a block "
        "/403 -- robust; `basic` is HTTP-only, `full_browser` always renders) [env WEB_PROFILE]",
    )
    sub.add_argument(
        "--full-browser",
        dest="profile",
        action="store_const",
        const="full_browser",
        help="shorthand for --profile full_browser",
    )
    sub.add_argument(
        "--proxy",
        default=_env("WEB_PROXY"),
        help="proxy URL for all traffic (http://[user:pass@]host:port) [env WEB_PROXY]",
    )
    sub.add_argument(
        "--browser-path",
        default=_env("WEB_BROWSER_PATH"),
        metavar="EXE",
        help="an explicit browser binary for any browser tier to launch [env WEB_BROWSER_PATH]",
    )
    sub.add_argument(
        "--no-cache", action="store_true", help="do not read/write the located-reference cache"
    )
    sub.add_argument(
        "--shim",
        action="store_true",
        default=_env_flag("WEB_LLM_SHIM"),
        help="use a REAL model via the local `claude -p` CLI (no API key): drives the LLM crawl "
        "frontier in `locate` + the query in `author` (--model is a CLI alias) [env WEB_LLM_SHIM]",
    )
    sub.add_argument(
        "--model",
        default=_env("WEB_LLM_MODEL"),
        help="LLM model id / CLI alias (else the default) [env WEB_LLM_MODEL]",
    )
    sub.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="stream every fetch too (default streams crawled pages + retries/escalations)",
    )


# -- web locate -----------------------------------------------------------------------------------


# -- web author -----------------------------------------------------------------------------------


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
    return AnthropicLlm(
        model=args.model or _env("WEB_LLM_MODEL") or PIPELINE_MODEL,
        rate=rate,
        pricing=pricing,
        system=SYSTEM,
        max_tokens=MAX_TOKENS,
    )


# -- web fetch / web resolve ----------------------------------------------------------------------


def _io_args(sub: argparse.ArgumentParser) -> None:
    """The operational flags for ``web fetch`` / ``web resolve``: a URL + the transport knobs (no
    brief, no LLM). Every option defaults from its ``WEB_*`` env var; a flag overrides it."""
    sub.add_argument("url", help="the URL to fetch / resolve")
    sub.add_argument(
        "--profile",
        default=_env("WEB_PROFILE", "basic_browser"),
        choices=("basic", "basic_browser", "full_browser"),
        help="transport profile (basic = HTTP-only; basic_browser escalates to a browser on a block; "
        "full_browser always renders) [env WEB_PROFILE]",
    )
    sub.add_argument(
        "--full-browser",
        dest="profile",
        action="store_const",
        const="full_browser",
        help="shorthand for --profile full_browser",
    )
    sub.add_argument(
        "--proxy",
        default=_env("WEB_PROXY"),
        help="proxy URL for all traffic (http://[user:pass@]host:port) [env WEB_PROXY]",
    )
    sub.add_argument(
        "--browser-path",
        default=_env("WEB_BROWSER_PATH"),
        metavar="EXE",
        help="an explicit browser binary for any browser tier to launch [env WEB_BROWSER_PATH]",
    )
    sub.add_argument(
        "-v", "--verbose", action="store_true", help="stream every transport step to stderr"
    )


def _fetch_profile(profile: str, proxy: "str | None", browser_path: "str | None") -> Profile:
    """A SINGLE transport identity for ``web fetch`` (one attempt, no escalation ladder): the cheap
    HTTP tier for ``basic``, else one browser render. ``--proxy`` / ``--browser-path`` pin on."""
    base = _fp.BASIC if profile == "basic" else _fp.BROWSER
    if proxy:
        base = base.with_(proxy=proxy)
    if browser_path and base.browser:
        base = base.with_(executable_path=browser_path)
    return base


async def _fetch(args: argparse.Namespace) -> int:
    """``web fetch <url>`` -- ONE transport fetch (httpx / curl_cffi, or a single browser render).
    The response BODY goes to stdout (pipeable); status / size / timing go to stderr."""
    prof = _fetch_profile(args.profile, args.proxy, args.browser_path)
    _err(f"fetching {args.url} [{'browser render' if prof.browser else 'HTTP'}]…")
    with _Progress(args.verbose):
        try:
            snap = await _fetch_one(args.url, profile=prof)
        except WebException as exc:
            _err(f"fetch failed: {exc}")
            return 1
    if (
        snap.error is not None
    ):  # a TRANSPORT failure (no response) -- a 4xx is still a valid snapshot
        _err(f"  transport error: {snap.error.code}: {snap.error.message}")
        return 1
    _err(f"  {snap.status} · {len(snap.content)} bytes · {snap.elapsed:.2f}s · {snap.url}")
    sys.stdout.buffer.write(snap.content)
    return 0 if snap.status and snap.status < 400 else 1


async def _resolve(args: argparse.Namespace) -> int:
    """``web resolve <url>`` -- the RESOLVE ladder (escalate transport -- HTTP -> browser -> realer
    browser -- until the page truly loads) -> a parsed Document. The body goes to stdout; the kind /
    size and the tier trace (retries / escalations) go to stderr."""
    resolver = _resolver(args.profile, args.proxy, args.browser_path)
    _err(f"resolving {args.url} [{args.profile}]…")
    try:
        with _Progress(args.verbose):
            doc = await resolver.resolve(args.url)
    except WebException as exc:
        _err(f"resolve failed: {exc}")
        return 1
    finally:
        await resolver.aclose()
    _err(f"  {doc.kind} · {len(doc.content)} bytes · {doc.url or args.url}")
    sys.stdout.buffer.write(doc.content)
    return 0


# -- onboard: the staged pipeline ------------------------------------------------------------------


def _state_path(args: argparse.Namespace, values: "dict[str, str]") -> Path:
    if args.state:
        return Path(args.state)
    tail = "-".join(v.lower().replace(" ", "_") for v in values.values())
    name = Path(args.brief).stem
    return Path(f"{name}{'-' + tail if tail else ''}.json")


async def _onboard(args: argparse.Namespace) -> int:
    """Run (or resume) the staged pipeline for a brief; print each stage's outcome, the spend per
    stage, and the authored query + sample rows."""
    values: dict[str, str] = {}
    for item in args.arg:
        name, sep, value = item.partition("=")
        if not sep:
            _err(f"--arg wants NAME=VALUE, got {item!r}")
            return 2
        values[name.strip()] = value.strip()
    if args.entity and "company" not in values:  # the positional entity is the usual argument
        values["company"] = args.entity
    brief = Brief.load(args.brief)
    path = _state_path(args, values)
    if path.is_file():
        state = Onboarding.load(path)
        if args.reset_from:
            state.reset_from(args.reset_from)
        _err(f"resuming {path} at {state.next_stage() or 'done'}")
    else:
        try:
            state = Onboarding.start(brief, **values)
        except BriefError as exc:
            _err(f"{exc} (pass it with --arg name=value)")
            return 2
    resolver = _resolver(args.profile, args.proxy, args.browser_path)
    llm = _build_llm(args)
    ctx = Context(resolver=resolver, llm=llm, search=default_search())
    try:
        with _Progress(args.verbose):
            await pipeline_run(state, ctx, until=args.until, save=path)
    finally:
        await resolver.aclose()
        await llm.aclose()
    _err("")
    for line in state.log:
        _err(f"  {line.stage:<17} {line.elapsed_s:>6.1f}s  {line.calls} call(s)  ${line.usd:.4f}")
    _err(
        f"  spend:      ${state.spend.usd:.4f} over {state.spend.calls} call(s) "
        f"({state.spend.chars_in} chars in / {state.spend.chars_out} out; the same calls on the "
        f"API at Haiku prices ≈ ${state.spend.api_estimate():.4f})"
    )
    _err(f"  state:      {path}")
    if state.stopped:
        _err(f"  stopped:    {state.stopped}")
    if state.expand is not None:
        src = state.expand
        _err(
            f"  source:     {src.url} ({src.kind}, {src.profile}; {', '.join(src.flags) or 'no flags'})"
        )
    if state.review_location is not None:
        _err(
            f"  location:   {'ok' if state.review_location.ok else 'CONCERN'} — {state.review_location.summary}"
        )
    ex = state.author_extract
    if ex is not None:
        _err(
            f"  rows:       {ex.row_count}"
            + (f"  misses: {', '.join(ex.misses)}" if ex.misses else "")
        )
        for row in ex.sample[: args.sample]:
            _err(f"    {json.dumps(row, ensure_ascii=False, default=str)[:400]}")
        if ex.blob:
            print(ex.blob)
    if state.author_review is not None:
        _err(
            f"  review:     {'ok' if state.author_review.ok else 'REJECTED'} — {state.author_review.notes}"
        )
    return 0 if (ex is not None and ex.complete and not state.stopped) else 1


# -- run: execute an authored query ----------------------------------------------------------------


async def _run_query(args: argparse.Namespace) -> int:
    """Run a query -- an onboarding state's authored query, or a blob -- and print the report and
    a sample of rows (``--lenient``: never abort on a missing field; each row says what it lacked).
    """
    path = Path(args.source)
    if path.is_file() and path.suffix == ".json":
        query = query_of(Onboarding.load(path))
        if query is None:
            _err(f"{path} holds no authored query yet")
            return 2
    else:
        query = Query.from_blob(path.read_text(encoding="utf-8") if path.is_file() else args.source)
    resolver = _resolver(args.profile, args.proxy, args.browser_path)
    try:
        with _Progress(args.verbose):
            result = await query.run(resolver, lenient=args.lenient)
    finally:
        await resolver.aclose()
    _err(f"  report:     {result.report.summary()}")
    for row in result.rows[: args.sample]:
        _err(f"    {json.dumps(row, ensure_ascii=False, default=str)[:400]}")
    for doc in result.documents[: args.sample]:
        _err(f"    document {doc.url} ({len(doc.content)} bytes, {doc.content_type})")
    print(json.dumps(result.rows, ensure_ascii=False, default=str))
    return 0 if result.report.ok else 1


# -- view: a stage's debug record + its contract -------------------------------------------------


def _view(args: argparse.Namespace) -> int:
    """Show an onboarding's state file: every stage's outcome at a glance, or ONE stage in full --
    its contract (the model it returned, as JSON), its log line, and every model exchange it made
    (the reply always; the prompt with ``--prompt``)."""
    path = Path(args.state)
    if not path.is_file():
        _err(f"no state file at {path}")
        return 2
    state = Onboarding.load(path)
    if args.stage is None:  # the overview
        print(f"brief: {state.brief.name or '(ad hoc)'} {state.brief.values}")
        print(
            f"next:  {state.next_stage() or 'done'}"
            + (f"  (stopped: {state.stopped})" if state.stopped else "")
        )
        for name in STAGE_NAMES:
            out = getattr(state, name)
            log = next((l for l in state.log if l.stage == name), None)
            calls = f"{log.calls} call(s) ${log.usd:.4f} {log.elapsed_s:.1f}s" if log else ""
            summary = _one_line(out) if out is not None else "(to do)"
            print(f"  {name:<17} {calls:<28} {summary}")
        print(
            f"spend: ${state.spend.usd:.4f} over {state.spend.calls} call(s); "
            f"API estimate ${state.spend.api_estimate():.4f}; {len(state.trace)} exchange(s) traced"
        )
        return 0
    if args.stage not in STAGE_NAMES:
        _err(f"unknown stage {args.stage!r}; stages are {', '.join(STAGE_NAMES)}")
        return 2
    out = getattr(state, args.stage)
    log = next((l for l in state.log if l.stage == args.stage), None)
    print(
        f"== {args.stage}: "
        + (
            f"{log.calls} call(s), ${log.usd:.4f}, {log.elapsed_s:.1f}s, {log.chars_in} chars in / {log.chars_out} out"
            if log
            else "not run"
        )
    )
    if log and log.note:
        print(f"   note: {log.note}")
    print("-- contract:")
    print(out.model_dump_json(indent=1) if out is not None else "(no output yet)")
    traces = [t for t in state.trace if t.stage == args.stage]
    print(f"-- model exchanges: {len(traces)}")
    for i, t in enumerate(traces, 1):
        if args.prompt:
            print(f"[{i}] PROMPT ({len(t.prompt)} chars):")
            print(t.prompt)
        print(f"[{i}] REPLY ({len(t.reply)} chars):")
        print(t.reply)
    return 0


def _one_line(model: BaseModel) -> str:
    """A stage output in one line for the overview."""
    data = model.model_dump()
    if "hits" in data:
        return f"{len(data['hits'])} hit(s) for {data.get('term')!r}"
    if "picks" in data and "dropped" in data:
        return f"{len(data['picks'])} pick(s): " + ", ".join(
            f"{p['tier']} {p['url']}" for p in data["picks"][:3]
        )
    if "visited" in data:
        return f"{len(data['visited'])} visited, {len(data['candidates'])} candidate(s), {len(data['reviews'])} reviewed; {data.get('note')}"
    if "present" in data:
        return f"{'present' if data['present'] else 'absent'} at {data.get('profile')}: {data.get('url')} — {data.get('reason')}"
    if "profile" in data and "flags" in data and "url" in data:
        return f"{data['kind']} at {data['profile']}; flags {', '.join(data['flags'])}; api {'yes' if data.get('api') else 'no'}"
    if "summary" in data:
        return f"{'ok' if data['ok'] else 'CONCERN'} — {data['summary']}"
    if "via_api" in data:
        return f"{'api' if data['via_api'] else 'page'} {data['url']} at {data['profile']}"
    if "attempts" in data:
        return (
            f"{'complete' if data['complete'] else 'INCOMPLETE'}, {data['row_count']} row(s); "
            + (data["attempts"][-1] if data["attempts"] else "")
        )
    if "notes" in data:
        return f"{'ok' if data['ok'] else 'REJECTED'} ({data.get('next')}) — {data['notes']}"
    return str(data)[:120]


# -- entry ----------------------------------------------------------------------------------------


def _llm_args(sub: argparse.ArgumentParser) -> None:
    """The model metering options (the API path): a rate limit + per-million-token prices."""
    sub.add_argument(
        "--rate",
        type=float,
        default=_env_float("WEB_LLM_RATE", 0.0),
        metavar="SECS",
        help="min seconds between LLM calls (a shared/corporate key) [env WEB_LLM_RATE]",
    )
    sub.add_argument(
        "--price-input",
        type=float,
        default=_env_float("WEB_PRICE_INPUT", 0.0),
        metavar="USD",
        help="input price ($/million tokens) -- for the spend report [env WEB_PRICE_INPUT]",
    )
    sub.add_argument(
        "--price-output",
        type=float,
        default=_env_float("WEB_PRICE_OUTPUT", 0.0),
        metavar="USD",
        help="output price ($/M tokens) [env WEB_PRICE_OUTPUT]",
    )
    sub.add_argument(
        "--price-cache-read",
        type=float,
        default=_env_float("WEB_PRICE_CACHE_READ", 0.0),
        metavar="USD",
        help="cache-read price ($/M tokens) [env WEB_PRICE_CACHE_READ]",
    )
    sub.add_argument(
        "--price-cache-write",
        type=float,
        default=_env_float("WEB_PRICE_CACHE_WRITE", 0.0),
        metavar="USD",
        help="cache-write price ($/M tokens) [env WEB_PRICE_CACHE_WRITE]",
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="web",
        description="Onboard a dataset for a brief: locate its source, author its query (staged, resumable).",
        epilog="e.g. web onboard ir-news --arg company=Intel --state intel.json",
    )
    subs = parser.add_subparsers(dest="cmd", required=True)

    fet = subs.add_parser(
        "fetch", help="ONE transport fetch of a URL -> its body (httpx / curl_cffi or a browser)"
    )
    _io_args(fet)

    res = subs.add_parser(
        "resolve", help="RESOLVE a URL through the escalation ladder -> a parsed document"
    )
    _io_args(res)

    onb = subs.add_parser(
        "onboard",
        help="the STAGED pipeline: search → review → crawl → review → expand → review → resolve "
        "→ extract → review, resumable from a state file (see webclient/onboard/PIPELINE.md)",
    )
    _transport_args(onb)
    _llm_args(onb)
    onb.add_argument(
        "--arg",
        action="append",
        default=[],
        metavar="NAME=VALUE",
        help="a brief argument (e.g. --arg company=Intel); the brief declares its args",
    )
    onb.add_argument(
        "--state",
        default=None,
        metavar="FILE",
        help="the onboarding state file: resumed when it exists, written after every stage "
        "(default: <brief>-<args>.json in the working directory)",
    )
    onb.add_argument("--until", default=None, metavar="STAGE", help="stop after this stage")
    onb.add_argument(
        "--reset-from", default=None, metavar="STAGE", help="redo this stage and every later one"
    )
    onb.add_argument(
        "--sample", type=int, default=5, metavar="N", help="how many rows to print (default 5)"
    )

    runp = subs.add_parser(
        "run",
        help="run an authored query (a state file or a blob) -> the report + the rows (JSON on stdout)",
    )
    runp.add_argument(
        "source", metavar="STATE|BLOB", help="an onboarding state file, a blob file, or a blob"
    )
    runp.add_argument(
        "--lenient",
        action="store_true",
        help="never abort on a missing field; rows say what they lacked",
    )
    runp.add_argument(
        "--sample", type=int, default=5, metavar="N", help="how many rows to print (default 5)"
    )
    runp.add_argument(
        "--profile",
        default=_env("WEB_PROFILE", "basic_browser"),
        choices=("basic", "basic_browser", "full_browser"),
    )
    runp.add_argument("--proxy", default=_env("WEB_PROXY"))
    runp.add_argument("--browser-path", default=_env("WEB_BROWSER_PATH"), metavar="EXE")
    runp.add_argument("-v", "--verbose", action="store_true")

    view = subs.add_parser(
        "view",
        help="show an onboarding state: every stage at a glance, or one stage's contract + exchanges",
    )
    view.add_argument(
        "state", metavar="FILE", help="the onboarding state file (web onboard --state)"
    )
    view.add_argument("stage", nargs="?", default=None, help="a stage name for the full record")
    view.add_argument(
        "--prompt", action="store_true", help="also print the prompts sent to the model"
    )
    return parser


def main(argv: "Sequence[str] | None" = None) -> int:
    args = _parser().parse_args(argv)
    if args.cmd == "view":
        return _view(args)
    runner = {"fetch": _fetch, "resolve": _resolve, "onboard": _onboard, "run": _run_query}[
        args.cmd
    ]

    async def _run() -> int:
        # Close the process-wide default pool on THIS run's loop before it ends: pooled backends
        # (notably curl_cffi's session) are bound to the loop they were created on, so leaving them
        # for a later asyncio.run() -- e.g. a second CLI call in one process -- crashes with a
        # cross-loop future. Closing here also cleans up any browser the run launched.
        try:
            return await runner(args)
        finally:
            await aclose_default_pool()

    return asyncio.run(_run())


if __name__ == "__main__":
    sys.exit(main())
