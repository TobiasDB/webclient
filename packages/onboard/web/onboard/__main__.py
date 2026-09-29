"""The ``web`` CLI -- the Locate and Author phases as two composable subcommands.

    web locate [find-brief options]        find WHERE the dataset is  -> a Reference
    web author (<url> | --ref -) [shape]   write the wq query that extracts it -> a query

Each subcommand takes its slice of the brief as options (the FIND slice for ``locate``, the SHAPE
slice for ``author``), mirroring :class:`~web.onboard.models.Brief`. Each prints its serialised
ARTIFACT to **stdout** (the Reference as JSON / the query as a portable ``wq`` blob) and the
human-readable REASONING to **stderr** (the flags with their evidence, the field schema, the query
rendered as a chain, the advisory notes). So the two compose cleanly over a pipe -- stdout carries
only the machine artifact::

    web locate --goal "board members" --seed https://acme.com/board \\
      | web author --ref - --field name --field role --run

The code interface is :func:`web.onboard.locate` + :func:`web.onboard.author`; this is a thin
wrapper over them. The model comes from ``--model`` / ``ANTHROPIC_API_KEY``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from importlib.resources import files
from pathlib import Path
from typing import Sequence

from web.fetch import Profile as FetchProfile
from web.fetch import WebException
from web.resolve import EscalationPolicy, Resolver, profiles

from .author import build_query
from .llm import AnthropicLlm, Pricing, RateLimit
from .locate import locate
from .models import Brief, Reference
from .search import DdgSearch


def _err(*lines: str) -> None:
    """Human-readable reasoning goes to stderr, so stdout stays a clean, pipeable artifact."""
    for line in lines:
        print(line, file=sys.stderr)


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


def _packaged_briefs() -> "list[str]":
    """The names of the briefs bundled with the package (``web/onboard/briefs/*.md``)."""
    try:
        root = files("web.onboard").joinpath("briefs")
        return sorted(p.name[:-3] for p in root.iterdir() if p.name.endswith(".md"))
    except Exception:
        return []


def _load_brief(arg: str) -> Brief:
    """Resolve ``--brief``: a path to a markdown file, else a packaged brief by name
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


def _pairs(items: "Sequence[str] | None") -> "dict[str, str]":
    """Parse repeated ``NAME=VALUE`` options into a mapping (a missing ``=`` maps to empty)."""
    out: dict[str, str] = {}
    for item in items or []:
        name, _, value = item.partition("=")
        out[name.strip()] = value.strip()
    return out


def _transport_args(sub: argparse.ArgumentParser) -> None:
    """The transport options shared by both subcommands (profile + proxy)."""
    sub.add_argument(
        "--profile",
        default="basic",
        choices=("basic", "basic_browser", "full_browser"),
        help="resolve profile: HTTP, escalate-to-browser, or always-render",
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
        default=None,
        help="proxy URL for all traffic (http://[user:pass@]host:port)",
    )
    sub.add_argument(
        "--browser-path",
        default=None,
        metavar="EXE",
        help="an explicit browser binary (driver/executable) for any browser tier to launch",
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
    if ref.record_selector:
        _err(f"  records:   {ref.record_selector}   (the repeating-row selector to extract)")
    if ref.pagination:
        _err(f"  pager:     {ref.pagination}   (the pipeline follows it)")
    if ref.flags:
        _err("  flags:     " + ", ".join(ref.flags) + "   (the conclusions that fired)")
    if ref.signals:
        _err("  evidence:  " + ", ".join(ref.signals))
    score = ref.detail.get("score")
    if score is not None:
        _err(
            f"  score:     {score}   (dataset-likeness × scrapability -- why this source ranked top)"
        )


def _locate_brief(args: argparse.Namespace) -> Brief:
    """The FIND-slice Brief: a ``--brief`` markdown file (frontmatter) as the base, with any
    explicitly-given CLI option overriding it (so a file supplies defaults, flags tune them).
    """
    base = _load_brief(args.brief) if args.brief else Brief()
    updates: dict[str, object] = {}
    if args.goal:
        updates["goal"] = args.goal
    if args.seed:
        updates["seeds"] = args.seed
    if args.candidate:
        updates["candidates"] = args.candidate
    if args.start_url:
        updates["start_url"] = args.start_url
    if args.search:
        updates["search"] = args.search
    if args.look:
        updates["look"] = args.look
    if args.ignore:
        updates["ignore"] = args.ignore
    if args.max_pages != 40:
        updates["max_pages"] = args.max_pages  # 40 is the shared default
    if args.no_prefer_api:
        updates["prefer_api"] = False
    return base.model_copy(update=updates)


async def _locate(args: argparse.Namespace) -> int:
    brief = _locate_brief(args)
    resolver = _resolver(args.profile, args.proxy, args.browser_path)
    # a default web-search backend (ddgs): only used when the brief gives no seeds/candidates, so
    # `--search "BBC latest news"` (or a company in --goal) turns into seed URLs.
    try:
        reference = await locate(brief, resolver=resolver, search=DdgSearch(k=args.search_k))
    except WebException as exc:
        _err(f"locate failed: {exc}")
        return 1
    finally:
        await resolver.aclose()
    if reference is None:
        _err(
            "no source holds the dataset (nothing scored above zero); "
            "try --seed/--candidate, or a --search term."
        )
        return 1
    _explain_reference(reference)  # reasoning -> stderr
    print(
        reference.model_dump_json()
    )  # the serialised Reference -> stdout (pipe into `author --ref -`)
    return 0


# -- web author -----------------------------------------------------------------------------------


def _reference_arg(args: argparse.Namespace) -> Reference:
    """The Reference to author over: ``--ref -`` reads a locate Reference (JSON) from stdin; ``--ref
    FILE`` from a file; else the positional URL builds a minimal Reference (Author resolves it and
    computes its own flags, so a bare URL works -- piping locate's Reference just carries its hints).
    """
    if args.ref == "-":
        return Reference.model_validate_json(sys.stdin.read())
    if args.ref:
        with open(args.ref, encoding="utf-8") as fh:
            return Reference.model_validate_json(fh.read())
    return Reference(url=args.url)


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


def _author_brief(args: argparse.Namespace) -> Brief:
    """The SHAPE-slice Brief: a ``--brief`` markdown file (frontmatter, incl. its ``schema:`` ->
    fields + descriptions) as the base, with any explicitly-given CLI option overriding it.
    """
    base = _load_brief(args.brief) if args.brief else Brief()
    updates: dict[str, object] = {}
    if args.goal:
        updates["goal"] = args.goal
    if args.field:
        updates["fields"] = args.field
    if args.describe:
        updates["descriptions"] = _pairs(args.describe)
    if args.select:
        updates["selectors"] = _pairs(args.select)
    if args.optional:
        updates["optional"] = args.optional
    if args.hints:
        updates["hints"] = args.hints
    if args.download:
        updates["download"] = True
    return base.model_copy(update=updates)


async def _author(args: argparse.Namespace) -> int:
    reference = _reference_arg(args)
    brief = _author_brief(args)
    resolver = _resolver(args.profile, args.proxy, args.browser_path)
    llm = _build_llm(args)
    try:
        query, engine, notes = await build_query(reference, brief, resolver=resolver, llm=llm)
        _explain_query(reference, brief, engine, query.describe(), notes)  # reasoning -> stderr
        print(query.to_blob())  # the serialised query -> stdout
        if getattr(llm, "calls", 0):  # metered spend (a real, priced client) -> stderr
            u = llm.usage
            _err(
                f"  spend:     ${llm.spent_usd:.4f} over {llm.calls} call(s)"
                f"  (tokens in {u.input}, out {u.output}, cache r/w {u.cache_read}/{u.cache_write})"
            )
        if not args.run:
            return 0
        rows = await query.acollect(resolver=resolver)
        listed = rows if isinstance(rows, list) else [rows]
        _err(f"\n  rows:      {len(listed)}")
        for row in listed[: args.sample]:
            _err(f"    {json.dumps(row, ensure_ascii=False)}")
        return 0 if listed else 1
    finally:
        await resolver.aclose()
        await llm.aclose()


def _build_llm(args: argparse.Namespace) -> AnthropicLlm:
    """The metered LLM client from CLI config: a rate limit (a shared key) and per-million-token
    prices (so the run reports real spend)."""
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
        prog="web", description="Locate a dataset and author its query."
    )
    subs = parser.add_subparsers(dest="cmd", required=True)

    loc = subs.add_parser("locate", help="find WHERE the dataset is (-> a Reference)")
    loc.add_argument(
        "--brief",
        default=None,
        metavar="FILE|NAME",
        help="a brief markdown file (YAML frontmatter), or a packaged name "
        "(news/products/people), as the base; options override it",
    )
    loc.add_argument("--goal", default=None, help="the dataset to find (free text)")
    loc.add_argument(
        "--seed",
        action="append",
        default=[],
        metavar="URL",
        help="seed URL to crawl (repeatable)",
    )
    loc.add_argument(
        "--candidate",
        action="append",
        default=[],
        metavar="URL",
        help="evaluate exactly this URL, skip crawling (repeatable)",
    )
    loc.add_argument("--start-url", default=None, help="one known source to seed the crawl from")
    loc.add_argument(
        "--search",
        default=None,
        help="a web-search query/qualifier (e.g. a company) -- searched (with the goal) via ddgs "
        "to seed the crawl when no --seed/--candidate is given",
    )
    loc.add_argument(
        "--search-k", type=int, default=6, metavar="N", help="how many search results to seed from"
    )
    loc.add_argument(
        "--look",
        action="append",
        default=[],
        metavar="TEXT",
        help="page guide: prefer (repeatable)",
    )
    loc.add_argument(
        "--ignore",
        action="append",
        default=[],
        metavar="TEXT",
        help="page guide: avoid (repeatable)",
    )
    loc.add_argument(
        "--max-pages",
        type=int,
        default=40,
        metavar="N",
        help="crawl page budget (default 40)",
    )
    loc.add_argument(
        "--no-prefer-api",
        action="store_true",
        help="do not prefer a live XHR/data-API over the page",
    )
    _transport_args(loc)

    aut = subs.add_parser("author", help="write the wq query that extracts the dataset")
    aut.add_argument(
        "--brief",
        default=None,
        metavar="FILE|NAME",
        help="a brief markdown file (YAML frontmatter, incl. schema:), or a packaged name "
        "(news/products/people), as the base; options override it",
    )
    aut.add_argument("url", nargs="?", help="the source URL (or use --ref)")
    aut.add_argument(
        "--ref",
        default=None,
        metavar="FILE",
        help="a locate Reference as JSON: '-' for stdin, else a file",
    )
    aut.add_argument("--goal", default=None, help="what to extract (free text)")
    aut.add_argument(
        "--field",
        action="append",
        default=[],
        metavar="NAME",
        help="a record field (repeatable)",
    )
    aut.add_argument(
        "--describe",
        action="append",
        default=[],
        metavar="NAME=DESC",
        help="what a field is (repeatable)",
    )
    aut.add_argument(
        "--select",
        action="append",
        default=[],
        metavar="NAME=CSS",
        help="an explicit selector override for a field (repeatable)",
    )
    aut.add_argument(
        "--optional",
        action="append",
        default=[],
        metavar="NAME",
        help="a field that may be absent (repeatable)",
    )
    aut.add_argument("--hints", default=None, help="structural guidance for the query author")
    aut.add_argument(
        "--download",
        action="store_true",
        help="harvest the file link(s), not parsed rows",
    )
    aut.add_argument("--model", default=None, help="LLM model id (else the default)")
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
        "--price-output",
        type=float,
        default=0.0,
        metavar="USD",
        help="output price ($/M tokens)",
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
        "--run",
        action="store_true",
        help="run the authored query and print a sample of rows",
    )
    aut.add_argument(
        "--sample",
        type=int,
        default=5,
        metavar="N",
        help="how many rows to print (default 5)",
    )
    _transport_args(aut)
    return parser


def main(argv: "Sequence[str] | None" = None) -> int:
    args = _parser().parse_args(argv)
    if args.cmd == "author" and not args.url and not args.ref:
        print("web author: give a URL or --ref", file=sys.stderr)
        return 2
    runner = {"locate": _locate, "author": _author}[args.cmd]
    return asyncio.run(runner(args))


if __name__ == "__main__":
    sys.exit(main())
