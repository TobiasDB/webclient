"""Case study: render a JS page in a real browser and read its events.

Site: https://quotes.toscrape.com/js/ -- the sandbox's JavaScript-rendered
variant: the quotes are injected by a script, so a static fetch sees none of them
and a browser render sees all ten.

What it shows:
  * static fetch -> 0 quotes (the DOM the server sent is a shell);
  * browser render -> 10 quotes, plus the navigation/console/DOM events the page
    produced, exposed on ``doc.events``;
  * ``browser="auto"``: escalation is deliberately *conservative* -- it fires on an
    empty / SPA-shell page, but this page ships a non-empty shell, so ``auto`` does
    not escalate it (returns 0 quotes);
  * ``browser="probe"``: the explicit "can I scrape this / what do I need"
    diagnostic -- it resolves *both* tiers and compares, so it catches exactly this
    injected-content case (was_browser_required + how many words the browser
    recovered), and returns the fuller browser document.

Features: live browser render, events / events_of, summary().runtime,
browser="probe".
Needs a Playwright chromium (``playwright install chromium``).
Run:  env/bin/python examples/live_browser_events.py
"""

from __future__ import annotations

from collections import Counter

from webclient import NavigationEvent, WebClient, WebException

JS_PAGE = "https://quotes.toscrape.com/js/"
UA = "webclient-examples/0.1"


def main() -> None:
    with WebClient(default_headers={"User-Agent": UA}, timeout=30) as wc:
        static = wc.fetch(JS_PAGE)  # no JS executed
        print(f"static fetch:   {len(static.select_all('.quote'))} quotes visible")

        # 'auto' is conservative and won't escalate this non-empty shell:
        auto = wc.fetch(JS_PAGE, browser="auto")
        print(f"browser='auto': {len(auto.select_all('.quote'))} quotes "
              f"(auto stayed static -- page shell is non-empty)")

        # 'probe': resolve both tiers and compare -> a definitive answer.
        probed = wc.fetch(JS_PAGE, browser="probe")
        p = probed.summary().probe
        print(f"browser='probe': {len(probed.select_all('.quote'))} quotes; "
              f"browser_required={p.was_browser_required} "
              f"render_gain={p.render_gain} words (JS injects the content)")
        wc.release(probed)

        live = wc.fetch(JS_PAGE, browser="always")  # force the browser tier
        print(f"browser='always': {len(live.select_all('.quote'))} quotes rendered")

        by_type = Counter(type(e).__name__ for e in live.events)
        print(f"events captured: {dict(by_type)}")
        for nav in live.events_of(NavigationEvent):
            print(f"  navigation -> status {nav.status_code} (source {nav.source})")

        runtime = live.summary().runtime
        if runtime is not None:
            print(f"runtime facet: is_spa={runtime.is_spa} framework={runtime.framework}")

        wc.release(live)  # return the browser page to the pool


if __name__ == "__main__":
    try:
        main()
    except WebException as exc:
        print(f"run failed: {exc.error.type} -- {exc} (retriable={exc.error.retriable})")
    except Exception as exc:  # e.g. no chromium installed
        print(f"browser example needs Playwright chromium: {type(exc).__name__}: {exc}")
