"""``python -m webclient.pipelines`` -- run the onboarding pipeline from the CLI.

Give it a reusable brief (a markdown file) and one or more companies; it searches,
crawls, evaluates and authors a tested lazy query for each, printing the result and
the LLM spend. The model comes from ``--model`` / ``ANTHROPIC_API_KEY`` (or any
Messages-API ``--base-url``); web search uses the optional ``ddgs`` package.

    python -m webclient.pipelines --brief briefs/product_catalogue.md \
        --company "Acme" --company "Globex" --budget 1.00 -v
"""

from __future__ import annotations

import argparse
import logging
import sys
from typing import Sequence

from ..surfaces import WebClient
from .llm import Budget, LlmClient
from .onboarding import Brief, OnboardingResult, ddg_search, onboard


def _print_result(result: OnboardingResult, *, show_steps: bool) -> None:
    mark = "OK " if result.ok else "-- "
    print(f"\n{mark}{result.company}: {'ready' if result.ok else result.reason}")
    if show_steps:
        for step in result.steps:
            print(f"    · {step}")
    if result.evaluation is not None:
        ev = result.evaluation
        print(f"    source:  {ev.url}  (queryable={ev.is_queryable}, scrapability={ev.scrapability})")
    if result.query is not None:
        q = result.query
        print(f"    query:   {q.describe}")
        print(f"    tested:  {q.tested}  rows={q.row_count}")
        if len(q.base_urls) > 1:
            print(f"    bases:   {', '.join(q.base_urls)}")
        print(f"    blob:    {q.blob}")
    print(f"    spent:   ${result.cost_usd:.4f}")


def main(argv: "Sequence[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m webclient.pipelines")
    parser.add_argument("--brief", required=True, help="path to a brief markdown file")
    parser.add_argument(
        "--company", action="append", default=[], metavar="NAME",
        help="a company to onboard (repeatable)",
    )
    parser.add_argument("--model", default=None, help="LLM model id (else the default)")
    parser.add_argument("--base-url", default=None, help="a Messages-API base URL")
    parser.add_argument("--budget", type=float, default=None, metavar="USD",
                        help="cap total LLM spend across the run")
    parser.add_argument("--max-pages", type=int, default=20, help="crawl page budget")
    parser.add_argument("--no-browser", action="store_true", help="static-only crawl")
    parser.add_argument("-v", "--verbose", action="count", default=0,
                        help="-v shows pipeline steps, -vv adds debug")
    args = parser.parse_args(argv)

    level = logging.WARNING if not args.verbose else (
        logging.INFO if args.verbose == 1 else logging.DEBUG
    )
    logging.basicConfig(level=level, format="%(message)s")

    if not args.company:
        parser.error("give at least one --company")
    brief = Brief.load(args.brief)
    print(f"brief: {brief.title or brief.name or args.brief} "
          f"({len(brief.fields)} field[s]) -> {len(args.company)} company(ies)")

    kwargs: dict[str, object] = {}
    if args.model:
        kwargs["model"] = args.model
    if args.base_url:
        kwargs["base_url"] = args.base_url
    budget = Budget(max_usd=args.budget)
    with WebClient() as wc, LlmClient(budget=budget, **kwargs) as llm:  # type: ignore[arg-type]
        if not llm.auth:
            parser.error("no API key -- set ANTHROPIC_API_KEY (or point --base-url at a gateway)")
        results = onboard(
            args.company, brief, wc=wc, llm=llm, search=ddg_search,
            max_pages=args.max_pages, browser=not args.no_browser, budget=budget,
        )
    for result in results:
        _print_result(result, show_steps=args.verbose > 0)
    ok = sum(r.ok for r in results)
    print(f"\ndone: {ok}/{len(results)} onboarded · spent ${budget.spent_usd:.4f}"
          f" over {budget.calls} call(s)")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
