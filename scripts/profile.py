"""A profiling harness: run a concurrent workload against the offline demo site and report
CPU time, RSS, tracemalloc top allocations and the pool's lease stats (roadmap N14/N15).

    env/bin/python scripts/profile.py                      # 200 fetches, 20 concurrent
    env/bin/python scripts/profile.py --n 1000 --concurrency 50 --browser
    env/bin/python scripts/profile.py --pyspy                # also record a py-spy flamegraph

The workload is deliberately the SAME plan the demos run (fetch -> select_all -> extract),
so a regression here is a regression users feel. Measure first, then optimise (N15).
"""

from __future__ import annotations

import argparse
import asyncio
import os
import resource
import subprocess
import sys
import time
import tracemalloc
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "demos"))


def rss_mb() -> float:
    usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return usage / (1024 * 1024) if sys.platform == "darwin" else usage / 1024


async def workload(base: str, n: int, concurrency: int, browser: bool) -> dict[str, float]:
    from webclient import AsyncWebClient, doc

    sem = asyncio.Semaphore(concurrency)
    errors = 0
    latencies: list[float] = []

    async with AsyncWebClient() as ac:
        async def one(i: int) -> None:
            nonlocal errors
            async with sem:
                t0 = time.perf_counter()
                try:
                    rows = await (
                        ac.lazy.fetch(base, browser="always" if browser else False)
                        .select_all(".card")
                        .extract(title=doc.select(".title").attr("text"))
                        .project()
                        .acollect()
                    )
                    if not rows:
                        errors += 1
                except Exception:  # noqa: BLE001 - counted, not raised
                    errors += 1
                latencies.append(time.perf_counter() - t0)

        started = time.perf_counter()
        await asyncio.gather(*(one(i) for i in range(n)))
        elapsed = time.perf_counter() - started
        stats = ac._the_engine().pool.stats().model_dump()
    latencies.sort()
    return {
        "elapsed_s": elapsed,
        "req_per_s": n / elapsed if elapsed else 0.0,
        "p50_ms": latencies[len(latencies) // 2] * 1000 if latencies else 0.0,
        "p95_ms": latencies[int(len(latencies) * 0.95)] * 1000 if latencies else 0.0,
        "errors": errors,
        **{f"pool_{k}": v for k, v in stats.items() if isinstance(v, (int, float))},
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=200)
    ap.add_argument("--concurrency", type=int, default=20)
    ap.add_argument("--browser", action="store_true")
    ap.add_argument("--top", type=int, default=10, help="tracemalloc top-N lines")
    ap.add_argument("--pyspy", action="store_true", help="wrap this run in py-spy record")
    args = ap.parse_args()

    if args.pyspy and "WEBCLIENT_PROFILE_CHILD" not in os.environ:
        out = ROOT / "profile.svg"
        cmd = ["py-spy", "record", "-o", str(out), "--", sys.executable, __file__,
               *[a for a in sys.argv[1:] if a != "--pyspy"]]
        env = {**os.environ, "WEBCLIENT_PROFILE_CHILD": "1"}
        rc = subprocess.call(cmd, env=env)
        print(f"flamegraph: {out}")
        return rc

    from webclient.lab import serve  # the offline lab site

    base = serve() + "/lab/shop"
    tracemalloc.start()
    cpu0 = time.process_time()
    result = asyncio.run(workload(base, args.n, args.concurrency, args.browser))
    cpu = time.process_time() - cpu0
    current, peak = tracemalloc.get_traced_memory()
    snapshot = tracemalloc.take_snapshot()
    tracemalloc.stop()

    print(f"workload: n={args.n} concurrency={args.concurrency} browser={args.browser}")
    for k, v in result.items():
        print(f"  {k:>14}: {v:.1f}" if isinstance(v, float) else f"  {k:>14}: {v}")
    print(f"  {'cpu_s':>14}: {cpu:.2f}")
    print(f"  {'rss_mb':>14}: {rss_mb():.1f}")
    print(f"  {'py_heap_mb':>14}: {current / 1e6:.1f} (peak {peak / 1e6:.1f})")
    print(f"top {args.top} allocation sites:")
    for stat in snapshot.statistics("lineno")[: args.top]:
        print(f"  {stat}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
