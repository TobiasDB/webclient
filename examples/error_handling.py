"""Case study: how errors are raised, caught and structured.

Deliberately *unfriendly* inputs -- this example exists to trigger failures and
show that each one surfaces as an actionable, structured error rather than a
stray traceback.

Site: https://httpbin.org/ -- it exists precisely to return chosen status codes
and delays -- plus two local guards (SSRF, select-miss).

What it shows:
  * loud-by-default: a non-2xx raises WebException(type, status_code, retriable);
  * lenient: optional=True returns a not-ok Document instead of raising;
  * transport failures (timeout, bad DNS) are retriable;
  * a missing selection / attribute returns a not-ok sub-result under RETURN,
    and raises LookupError under RAISE;
  * the SSRF guard refuses private/loopback hosts by policy.

Features: WebException, error/optional policy, RETURN/RAISE, block_private_hosts.
Run:  env/bin/python examples/error_handling.py
"""

from __future__ import annotations

from webclient import RAISE, RETURN, WebClient, WebException

UA = "webclient-examples/0.1 (+https://github.com/TobiasDB/webclient)"


def case(label, fn) -> None:
    print(f"\n• {label}")
    try:
        fn()
    except WebException as exc:
        e = exc.error
        print(f"    WebException  type={e.type} status={e.status_code} "
              f"retriable={e.retriable}")
    except LookupError as exc:
        print(f"    LookupError   {exc}")


def main() -> None:
    with WebClient(default_headers={"User-Agent": UA}, timeout=20) as wc:
        case("404 -> raises by default (loud)",
             lambda: wc.fetch("https://httpbin.org/status/404"))

        def optional_404() -> None:
            d = wc.fetch("https://httpbin.org/status/404", optional=True)
            print(f"    not-ok doc    ok={d.ok} status={d.status_code} "
                  f"error={d.error.type if d.error else None}")
        case("404 -> optional=True returns a not-ok Document", optional_404)

        case("500 -> retriable server error",
             lambda: wc.fetch("https://httpbin.org/status/500"))

        case("403 -> treated as a block/challenge",
             lambda: wc.fetch("https://httpbin.org/status/403"))

        case("bad DNS -> retriable transport error",
             lambda: wc.fetch("https://no-such-host-9zx1.example/"))

        def select_miss_return() -> None:
            d = wc.fetch("https://httpbin.org/html")
            sub = d.select(".nope", error=RETURN)
            print(f"    RETURN miss   ok={sub.ok} error={sub.error.type if sub.error else None}")
        case("select miss (RETURN) -> not-ok sub-document", select_miss_return)

        case("select miss (RAISE) -> LookupError",
             lambda: wc.fetch("https://httpbin.org/html").select(".nope", error=RAISE))

    with WebClient(default_headers={"User-Agent": UA}, block_private_hosts=True) as guarded:
        case("SSRF guard -> loopback refused by policy",
             lambda: guarded.fetch("http://127.0.0.1:9/x"))


if __name__ == "__main__":
    main()
