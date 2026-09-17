"""The ``onboard`` CLI -- run the onboarding pipeline over one brief and N companies.

    onboard <brief> <company> [<company> ...] [options]
    onboard product-catalogue Acme Globex Initech --budget 1.00 -v

``<brief>`` is a reusable brief: a path to a markdown file, or the name of a packaged
brief under ``webclient/pipelines/briefs`` (e.g. ``product-catalogue``). It searches,
crawls, evaluates and authors a tested query for each company, printing the source,
the query and the spend. The model comes from ``--model`` / ``ANTHROPIC_API_KEY`` (or
any Messages-API ``--base-url``); web search uses the optional ``ddgs`` package.

Also runnable as ``python -m webclient.pipelines``.
"""

from __future__ import annotations

import argparse
import logging
import sys
from importlib.resources import files
from pathlib import Path
from typing import Sequence, cast

from ..surfaces import WebClient
from .llm import Budget, LlmClient, cheapest_model
from .onboarding import Brief, ddg_search, onboard


def _build_llm(args: argparse.Namespace, budget: Budget, parser: argparse.ArgumentParser) -> LlmClient:
    """The LLM client for this run. ``--shim`` routes the Messages API through local
    ``claude -p`` in process (no API key, no server; the cheapest model) for TESTING;
    otherwise the real Anthropic API (or any ``--base-url`` gateway), which needs a key."""
    if args.shim:
        # the shim helper lives in scripts/ (a dev/testing tool, not library code); add it to
        # the path only for this opt-in flag so the package stays import-clean otherwise.
        scripts = Path(__file__).resolve().parents[2] / "scripts"
        sys.path.insert(0, str(scripts))
        from claude_llm_adapter import claude_shim_client  # type: ignore[import-not-found]

        return cast(LlmClient, claude_shim_client(model=args.model, budget=budget))
    kwargs: dict[str, object] = {"min_interval": args.rate, "model": args.model or cheapest_model()}
    if args.base_url:
        kwargs["base_url"] = args.base_url
    llm = LlmClient(budget=budget, **kwargs)  # type: ignore[arg-type]
    if not llm.auth:
        parser.error("no API key -- set ANTHROPIC_API_KEY (or point --base-url at a gateway), "
                     "or use --shim to route through local `claude -p`")
    return llm


def _load_brief(arg: str) -> Brief:
    """Resolve ``<brief>``: a path to a markdown file, else a packaged brief by name
    (``webclient/pipelines/briefs/<name>.md``; ``-`` and ``_`` interchangeable)."""
    path = Path(arg)
    if path.is_file():
        return Brief.load(str(path))
    for name in {arg, arg.replace("-", "_"), arg.replace("_", "-")}:
        res = files("webclient.pipelines").joinpath(f"briefs/{name}.md")
        if res.is_file():
            return Brief.from_markdown(res.read_text(encoding="utf-8"))
    raise SystemExit(
        f"no brief {arg!r}: not a file, and no packaged briefs/{arg}.md. "
        "Available: " + ", ".join(_packaged_briefs()) or "(none)"
    )


def _packaged_briefs() -> "list[str]":
    try:
        root = files("webclient.pipelines").joinpath("briefs")
        return sorted(p.name[:-3] for p in root.iterdir() if p.name.endswith(".md"))
    except Exception:
        return []


def main(argv: "Sequence[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(
        prog="onboard",
        description="Onboard companies for a dataset brief.",
        epilog="e.g. onboard product-catalogue Acme Globex --budget 1.00 -v",
    )
    parser.add_argument("brief", help="a brief markdown file, or a packaged brief name")
    parser.add_argument("companies", nargs="+", metavar="company",
                        help="one or more companies to onboard")
    parser.add_argument("--model", default=None, help="LLM model id (else the default)")
    parser.add_argument("--base-url", default=None, help="a Messages-API base URL")
    parser.add_argument("--shim", action="store_true",
                        help="route the Messages API through local `claude -p` in process "
                             "(no API key/server; cheapest model) -- for TESTING")
    parser.add_argument("--budget", type=float, default=None, metavar="USD",
                        help="cap total LLM spend across the run")
    parser.add_argument("--rate", type=float, default=0.0, metavar="SECS",
                        help="rate-limit the LLM: min seconds between calls")
    parser.add_argument("--max-pages", type=int, default=20, help="crawl page budget")
    parser.add_argument("--no-browser", action="store_true", help="static-only crawl")
    parser.add_argument("--proxy", default=None,
                        help="proxy URL for ALL http + browser traffic (http://[user:pass@]host:port)")
    parser.add_argument("--review", action="store_true",
                        help="run the meta-review stage (grades the run; costs extra calls)")
    parser.add_argument("-v", "--verbose", action="count", default=0,
                        help="-v shows pipeline steps, -vv adds debug")
    args = parser.parse_args(argv)

    # keep the root (and thus httpx/httpcore) quiet; show only the pipeline's progress
    # -- always at INFO, -v adds debug detail.
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    logging.getLogger("webclient.pipelines").setLevel(
        logging.DEBUG if args.verbose else logging.INFO
    )

    brief = _load_brief(args.brief)
    log = logging.getLogger("webclient.pipelines")  # shows at INFO alongside the pipeline logs
    log.info("brief: %s (%d field[s]) -> %d company(ies)",
             brief.title or brief.name or args.brief, len(brief.fields), len(args.companies))

    budget = Budget(max_usd=args.budget)
    llm = _build_llm(args, budget, parser)
    from ..core.reference.models import BrowserConfig
    with WebClient(browser_config=BrowserConfig(proxy=args.proxy)) as wc, llm:
        results = onboard(
            args.companies, brief, wc=wc, llm=llm, search=ddg_search,
            max_pages=args.max_pages, browser=not args.no_browser, budget=budget,
            review=args.review,
        )
    # the pipeline logs each company's full summary (source / scores / flags /
    # reference / resolve / query + sample table / spend); here we add only the totals.
    ok = sum(r.ok for r in results)
    log.info("done: %d/%d onboarded · spent $%.4f over %d call(s)",
             ok, len(results), budget.spent_usd, budget.calls)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
