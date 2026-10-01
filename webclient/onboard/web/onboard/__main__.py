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
from typing import Protocol, cast, runtime_checkable

from pydantic import JsonValue
from web.crawl import CrawlEvent, FrontierMiddleware
from web.dsl import from_blob
from web.fetch import Event, EventBus, FetchEvent, Profile, WebException, aclose_default_pool
from web.fetch import fetch as _fetch_one
from web.fetch import profiles as _fp
from web.fetch import using
from web.resolve import ResolveEvent, Resolver

from .author import AuthorEvent, build_query
from .author_loop import ENGINES, write_query
from .compile import Query, QueryError
from .config import build_resolver
from .config import env as _env
from .config import env_flag as _env_flag
from .config import env_float as _env_float
from .frontier import llm_frontier
from .llm import AnthropicLlm, Llm, LlmEvent, Pricing, RateLimit, ReasonEvent, Usage
from .locate import locate
from .models import Brief, QueryArtifact, Reference
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
        #: the author loop's OUTCOME (from its ``done`` AuthorEvent): the row count its final query
        #: sampled, a one-row preview, and the last unresolved error -- so the CLI reports clearly.
        self.author_rows = 0
        self.author_sample = ""
        self.author_error = ""
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
            u = event.usage  # show the token breakdown so the cost (usage x price) is verifiable
            _err(
                f"  · llm [{event.model}] call {event.calls}: "
                f"in={u.input} out={u.output} cache_r={u.cache_read} cache_w={u.cache_write} tok "
                f"→ ${event.cost_usd:.4f}  (running ${event.spent_usd:.4f})"
            )
        elif isinstance(event, ReasonEvent):  # WHY a choice was made
            subj = f"{event.subject} — " if event.subject else ""  # full URL/subject, not truncated
            _err(f"  ⋯ {event.stage}: {subj}{event.text}")
        elif isinstance(event, AuthorEvent):  # the authoring stages
            if event.phase == "sample":
                fl = ", ".join(event.flags) or "—"
                _err(f"  · sample: {event.kind}, {event.lines} skeleton line(s), flags: {fl}")
            elif event.phase == "reply":
                _err(
                    f"  · model wrote: {' '.join(event.reply.split())}"
                )  # the FULL query, untruncated
            elif event.phase == "parsed":
                _err("  · query parsed + rerooted at the source")
            elif event.phase == "done":  # the loop's outcome -- rows sampled + a preview
                self.author_rows = event.rows
                self.author_sample = event.sample
                self.author_error = event.reply
                mark = "✓" if event.rows else "✗"
                _err(
                    f"  {mark} sampled {event.rows} row(s)"
                    + (f": {event.sample}" if event.sample else "")
                )
        elif isinstance(
            event, ResolveEvent
        ):  # transport fallbacks: which tier a fetch escalated to
            d = event.detail
            if event.phase == "escalate":
                remedy = f" ({d['remedy']})" if d.get("remedy") else ""
                _err(f"  · escalate → tier {d.get('tier')}{remedy}: {event.url}")
            elif event.phase == "sticky":
                _err(f"  · sticky → tier {d.get('tier')} (domain already needed it): {event.url}")
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


def _packaged_briefs() -> "list[str]":
    """The names of the briefs bundled with the package (``web/onboard/briefs/*.md``)."""
    try:
        root = files("web.onboard").joinpath("briefs")
        return sorted(p.name[:-3] for p in root.iterdir() if p.name.endswith(".md"))
    except (ModuleNotFoundError, FileNotFoundError, OSError):  # no package data -> just no names
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


def _record_verbs(brief_arg: str, url: str, engine: str, art: QueryArtifact) -> Path:
    """Append this run's DSL-verb tally (and the unknown verbs -- the gaps) to the persistent
    ``verbs.jsonl`` next to the located-reference cache, so gaps show up ACROSS runs."""
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    path = Path(base) / "web-onboard" / "verbs.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    line = {
        "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "brief": brief_arg,
        "url": url,
        "engine": engine,
        "complete": art.complete,
        "verbs": art.verbs,
        "unknown": art.unknown_verbs,
    }
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(line, ensure_ascii=False) + "\n")
    return path


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
    _explain_evaluation(ref)


def _explain_evaluation(ref: Reference) -> None:
    """The EVALUATE stage's verdict on the chosen source (it rides on ``Reference.detail``): the
    select tier, scrapability, whether it is a queryable data source, the model's one-line reason,
    and where the most recent records are."""
    d = ref.detail
    if d.get("verdict"):
        _err(
            f"  evaluate:  [{d.get('tier', '')}] scrapability {d.get('scrapability', '?')}/10, "
            f"queryable={d.get('queryable', False)} — {d.get('verdict')}"
        )
    if d.get("recency_hint"):
        _err(f"  recency:   {d['recency_hint']}")


def _entity_arg(entity: "str | None") -> str:
    """The entity as it would be retyped on the command line (quoted if it has spaces)."""
    return "" if not entity else (f'"{entity}"' if " " in entity else entity)


async def _locate(args: argparse.Namespace) -> int:
    brief = _apply_entity(_load_brief(args.brief), args.entity)
    resolver = _resolver(args.profile, args.proxy, args.browser_path)
    # The LLM drives the CRAWL FRONTIER (each round it picks which pending edges to expand toward the
    # dataset) AND the FINAL REVIEW (it judges candidates best-first, confirming the page holds the
    # dataset for the entity). It is used whenever one is available -- the Anthropic API by default
    # (a key is set), `claude -p` under --shim -- and Locate falls back to a deterministic crawl +
    # heuristic scoring only when NEITHER is configured. So a plain `web locate` with a key set DOES
    # use the model; --shim just switches the backend.
    llm = _locate_llm(args)
    frontier: "tuple[FrontierMiddleware, ...]" = ()
    if llm is not None:
        frontier = (
            llm_frontier(
                llm,
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
        + (" · LLM frontier+review" if llm is not None else " · deterministic (no LLM configured)")
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
                review=llm,
            )
    except WebException as exc:
        _err(f"locate failed: {exc}")
        return 1
    finally:
        await resolver.aclose()
        if llm is not None:
            await llm.aclose()
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


def _cell(value: object) -> str:
    """One table cell -- JSON for a nested value, whitespace COLLAPSED for a string (a newline in a
    value would break the alignment), truncated so the table stays legible."""
    text = (
        json.dumps(value, ensure_ascii=False, default=str)
        if isinstance(value, (dict, list))
        else " ".join(str(value).split())
    )
    return text if len(text) <= 40 else text[:39] + "…"


def _render_table(rows: "Sequence[JsonValue]", max_rows: int = 5) -> "list[str]":
    """The sample rows as an aligned text table -- the columns are the row keys (a non-dict row
    falls back to a single ``value`` column)."""
    shown = list(rows[:max_rows])
    if not shown:
        return ["    (no rows)"]
    dicts = [r if isinstance(r, dict) else {"value": r} for r in shown]
    cols: list[str] = []
    for d in dicts:
        cols += [k for k in d if k not in cols]
    width = {c: max([len(c)] + [len(_cell(d.get(c, ""))) for d in dicts]) for c in cols}

    def row(vals: "list[str]") -> str:
        return "    " + "  ".join(f"{v:<{width[c]}}" for c, v in zip(cols, vals))

    return [
        row(cols),
        row(["-" * width[c] for c in cols]),
        *[row([_cell(d.get(c, "")) for c in cols]) for d in dicts],
    ]


def _summarize_author(
    reference: Reference, brief: Brief, art: QueryArtifact, *, spent: "tuple[float, int] | None"
) -> None:
    """The always-printed end-of-run summary: the outcome, the chosen source with its evaluation
    + flags (with the evidence behind them), the transport to reproduce the fetch, the query with
    its test verdict (rows / timeliness / absent fields), a SAMPLE TABLE, the rejection trail that
    led to it, the portable blob(s), and the spend. Enough to judge it -- or re-run it by hand."""
    ok = art.complete and art.row_count > 0
    if ok and art.review:  # extracts, but the reviewer rejected the final sample -- not "ready"
        outcome = "extracts, but the sample was REJECTED by the review (see review: below)"
    elif ok:
        outcome = "ready"
    elif art.reason.startswith("one-shot"):
        outcome = "authored, not tested (one-shot; run with --run to test it)"
    else:
        outcome = f"not ready — {art.reason}" + (
            f"; required field(s) absent from the source: {', '.join(art.absent)}"
            if art.absent
            else ""
        )
    d = reference.detail
    lines = [
        "",
        "── author " + "─" * 46,
        f"  result:    {outcome}",
        f"  source:    {reference.url}",
    ]
    if reference.api_endpoint:
        lines.append(
            f"  data-API:  {reference.api_endpoint}  ← the JSON behind the page (query this, not the DOM)"
        )
    if d.get("verdict"):
        lines.append(
            f"  evaluate:  [{d.get('tier', '')}] scrapability {d.get('scrapability', '?')}/10, "
            f"queryable={d.get('queryable', False)} — {d.get('verdict')}"
        )
    if d.get("recency_hint"):
        lines.append(f"  recency:   {d['recency_hint']}")
    lines.append(
        f"  transport: {reference.profile or 'basic'}"
        + ("   (needs a browser to render)" if reference.needs_browser else "")
    )
    if reference.record_selector:
        lines.append(f"  records:   {reference.record_selector}")
    if reference.pagination:
        lines.append(f"  pager:     {reference.pagination}   (the pipeline follows it)")
    if reference.assessment:  # each conclusion + the evidence that fired it
        lines.append("  flags:")
        for f in reference.assessment:
            lines.append(f"    {f.name} ({f.confidence:.2f}) — {f.description}")
            lines += [f"      · {sg.name} ({sg.confidence:.2f})" for sg in f.signals]
    if brief.fields:
        lines.append(
            "  schema:    "
            + ", ".join(f + ("*" if f in brief.optional else "") for f in brief.fields)
            + "   (* = optional)"
        )
    split = len(art.sections) > 1
    lines.append(
        f"  query:     {art.describe}"
        + (f"   (split: {len(art.sections)} sections, rows concatenated)" if split else "")
    )
    lines.append(
        f"  tested:    {'✓' if art.tested else '✗'}  {art.row_count} row(s)"
        + (" combined" if split else "")
    )
    if art.review:  # the reviewer rejected the FINAL sample -- say so next to the row count
        lines.append(f"  review:    ✗ rejected — {art.review}")
    if art.verbs:  # the DSL verbs the model reached for -- and the ones the DSL lacks (gaps)
        used = ", ".join(f"{v}×{n}" for v, n in art.verbs.items())
        lines.append(f"  verbs:     {used}")
        if art.unknown_verbs:
            lines.append(
                f"  ⚠️ unknown verbs (DSL gaps): {', '.join(art.unknown_verbs)}   "
                "(the model wanted these; the DSL has no such verb)"
            )
    if art.timeliness:  # a FLAG for the human: is the newest extracted row recent?
        lines.append(f"  timeliness:{' ⚠️ STALE —' if art.stale else ' ✓'} {art.timeliness}")
    if art.absent:
        lines.append(
            f"  absent:    {', '.join(art.absent)}   (required field(s) the source does not carry)"
        )
    lines.append("  sample:")
    lines += _render_table(art.sample)
    if art.attempts:  # the rejection trail: why each earlier attempt was rejected
        lines.append(f"  authoring: {len(art.attempts) + 1} attempt(s); earlier rejections:")
        lines += [f"    ✗ {a}" for a in art.attempts]
    if split:
        lines.append(f"  section blobs ({len(art.sections)}; run each + concatenate the rows):")
        for i, sec in enumerate(art.sections):
            lines += [
                f"    section {i + 1}: {sec.describe}   ({sec.row_count} row[s])",
                f"    {sec.blob}",
            ]
    else:
        lines += ["  query blob (stdout; runnable with `run_blob(blob)`):", f"    {art.blob}"]
    if spent is not None:
        lines.append(f"  spent:     ${spent[0]:.4f} over {spent[1]} call(s)")
    _err(*lines)


async def _reference_for_author(
    args: argparse.Namespace, brief: Brief, resolver: Resolver, reviewer: "Llm | None"
) -> "Reference | None":
    """The Reference to author over, easiest-first: an explicit ``--ref`` (stdin/file, the pipe
    form); else the cached Reference from a prior ``web locate <brief> [entity]`` (the run-separately
    form); else locate it now (so ``web author`` works standalone). ``brief`` already has the entity
    applied; ``reviewer`` is the LLM used for Locate's final candidate review when we locate here.
    """
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
    if reviewer is not None:
        frontier = (
            llm_frontier(
                reviewer,
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
            review=reviewer,
        )


async def _author(args: argparse.Namespace) -> int:
    brief = _apply_entity(_load_brief(args.brief), args.entity)
    resolver = _resolver(args.profile, args.proxy, args.browser_path)
    llm = _build_llm(args)
    try:
        reference = await _reference_for_author(args, brief, resolver, reviewer=llm)
        if reference is None:
            _err("no source located to author over (nothing scored above zero).")
            return 1
        if not args.simple:  # DEFAULT: the conversation-driven authoring loop
            _err(f"authoring (loop): {reference.url}…")
            with _Progress(args.verbose):
                art = await write_query(
                    reference,
                    brief,
                    resolver=resolver,
                    llm=llm,
                    review=llm,
                    entity=args.entity or "",
                    engine=args.engine,
                )
            if not art.blob:
                _report_author_failure(art, args.verbose)
                return 1
        else:
            _err(f"authoring (one-shot): resolving {reference.url} then asking the model…")
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
            art = QueryArtifact(
                blob=query.to_blob(),
                describe=query.describe(),
                reason=f"one-shot ({engine})",
                attempts=notes,
            )
        spent = (llm.spent_usd, llm.calls) if isinstance(llm, _Metered) else None
        _summarize_author(reference, brief, art, spent=spent)  # the summary -> stderr
        if art.verbs:  # the persistent verb record: every run appends what the model reached for
            _err(f"  verb log:  {_record_verbs(args.brief, reference.url, args.engine, art)}")
        blobs = [sec.blob for sec in art.sections] or [art.blob]
        for blob in blobs:  # each section's serialised query -> stdout (newline-separated)
            print(blob)
        if not args.run:
            return 0 if (art.complete or args.simple) else 1
        _err(f"running the quer{'ies' if len(blobs) > 1 else 'y'}…")
        listed: "list[object]" = []
        try:
            with _Progress(args.verbose):
                for blob in blobs:  # run every section and CONCATENATE the rows
                    rows = await cast(Query, from_blob(blob)).acollect(resolver=resolver)
                    listed.extend(rows if isinstance(rows, list) else [rows])
        except WebException as exc:  # a runtime extraction failure (e.g. a loud select miss)
            _err(f"running failed: {exc}")
            return 1
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


def _report_author_failure(art: QueryArtifact, verbose: bool) -> None:
    """Explain WHY authoring produced no query -- the bare stop reason is not actionable, so name
    the real cause: an LLM / transport failure (a config problem, usually) vs a model that never
    wrote a valid query (its rejections are listed), each with the likely fix."""
    reason = art.reason
    if reason.startswith("error"):  # a turn raised -- an LLM / transport / config failure
        detail = reason.partition(":")[2].strip() or "an internal error"
        hint = (
            "  the local `claude -p` call failed or timed out (retried) — raise WEB_LLM_TIMEOUT "
            "(per call, seconds) / WEB_LLM_RETRIES, or re-run; the page itself is fine."
            if "llm.shim" in detail
            else "  this is an LLM/transport failure, not a bad page — check WEB_LLM_API_KEY / "
            "WEB_LLM_BASE_URL / WEB_LLM_MODEL (or pass --shim to use the local `claude -p` model)."
        )
        _err(f"authoring failed: {detail}", hint)
        return
    if reason.startswith("js_gated"):  # ground truth: a JS app fetched at the HTTP tier, no records
        _err(
            f"authoring stopped: {reason.partition(':')[2].strip()}",
            "  this is a LOCATE mis-tiering, not an authoring problem — the source needs a browser "
            "render: re-run `web locate` (it should bake full_browser) or pass --full-browser.",
        )
        return
    if reason in ("stalled", "budget"):  # the model tried but never wrote a valid query
        _err(
            f"authoring failed ({reason}): the model could not write a valid wq query for this "
            "page (every attempt was rejected or matched no records):",
            *[f"    ✗ {a}" for a in art.attempts],
            *([] if verbose else ["  re-run with -v to see the model's replies in full."]),
        )
        return
    _err(f"authoring failed ({reason}): no query was produced.")


def _locate_llm(args: argparse.Namespace) -> "AnthropicLlm | ClaudeShim | None":
    """The LLM that drives Locate's crawl FRONTIER + candidate REVIEW. Backend, in order: ``--shim``
    -> the local ``claude -p`` model (a real model, NO key); else the Anthropic API when a key is
    configured (``WEB_LLM_API_KEY`` / ``ANTHROPIC_API_KEY``, the ambient default); else ``None`` ->
    Locate runs DETERMINISTICALLY (breadth-first crawl + heuristic scoring, no key required). So the
    LLM is used whenever one is actually available -- ``--shim`` is only a backend switch, not the
    on/off for the LLM. (The rate limit comes from ``WEB_LLM_RATE`` via each client's gate.)"""
    if args.shim:
        return ClaudeShim(model=args.model) if args.model else ClaudeShim()
    if _env("WEB_LLM_API_KEY") or _env("ANTHROPIC_API_KEY"):
        return AnthropicLlm(model=args.model) if args.model else AnthropicLlm()
    return None


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


# -- entry ----------------------------------------------------------------------------------------


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="web",
        description="Locate a dataset and author its query -- driven by a brief.",
        epilog="e.g. web locate news BBC   then   web author news BBC --run",
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
        default=_env_float("WEB_LLM_RATE", 0.0),
        metavar="SECS",
        help="min seconds between LLM calls (a shared/corporate key) [env WEB_LLM_RATE]",
    )
    aut.add_argument(
        "--price-input",
        type=float,
        default=_env_float("WEB_PRICE_INPUT", 0.0),
        metavar="USD",
        help="input price ($/million tokens) -- for the spend report [env WEB_PRICE_INPUT]",
    )
    aut.add_argument(
        "--price-output",
        type=float,
        default=_env_float("WEB_PRICE_OUTPUT", 0.0),
        metavar="USD",
        help="output price ($/M tokens) [env WEB_PRICE_OUTPUT]",
    )
    aut.add_argument(
        "--price-cache-read",
        type=float,
        default=_env_float("WEB_PRICE_CACHE_READ", 0.0),
        metavar="USD",
        help="cache-read price ($/M tokens) [env WEB_PRICE_CACHE_READ]",
    )
    aut.add_argument(
        "--price-cache-write",
        type=float,
        default=_env_float("WEB_PRICE_CACHE_WRITE", 0.0),
        metavar="USD",
        help="cache-write price ($/M tokens) [env WEB_PRICE_CACHE_WRITE]",
    )
    aut.add_argument(
        "--simple",
        action="store_true",
        help="use the one-shot author instead of the default AGENT LOOP (the loop verifies the data "
        "is present, authors, reviews each sample vs the brief/entity, and repairs a failed query)",
    )
    aut.add_argument(
        "--engine",
        choices=ENGINES,
        default=os.environ.get("WEB_AUTHOR_ENGINE") or "steps",
        help="how the loop writes the query: 'steps' (default) = one op per turn (records / field "
        "/ detail / ...) with the result of each step fed back; 'chain' = the whole wq chain per "
        "turn [env WEB_AUTHOR_ENGINE]",
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
    runner = {"locate": _locate, "author": _author, "fetch": _fetch, "resolve": _resolve}[args.cmd]

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
