# Scaling, performance and resiliency — design (roadmap N15–N17)

*Status: shipped 2026-09-23 (Phase 7). Measure first, then tune: every knob below is
observable before it is adjustable.*

## What bounds a single webclient

One `WebClient` owns one **engine**: an asyncio loop thread, a **transport pool** (httpx
clients, browser pages), the event bus and the script registry. Every session on the
client shares that engine. The pool is the only place concurrency is created, so the
bottleneck of a single process is, by construction, the browser (`pool_pages`, default 4)
or the network (`pool_http`, default 10) — never the Python side, which is async-first.

Observability of that pool is built in:

- `ResourceEvent`s on the bus (`source="pool"`): `wait` (a lease had to queue),
  `created` (a new transport client), `exhausted` (no lease within `acquire_timeout`),
  `quota` (a session at its page quota). They ride the same stream as everything else, so
  a trace of a slow run shows *where* it waited.
- `wc.resources()` / `GET /health` → pool occupancy (per kind and **per owner**), RSS, the
  error-ledger size, loops waiting, the event cursor.
- `scripts/profile.py` runs a concurrent extract workload against the lab and reports
  throughput, p50/p95, pool stats, CPU, RSS and tracemalloc's top allocation sites
  (`--pyspy` for a flamegraph).

## Fairness: sessions cannot starve each other

`Settings.limits.session_pages` (env `WEBCLIENT_LIMITS__SESSION_PAGES`) caps the browser
pages **one session** may hold at once. A session at its quota waits on its *own* release,
not on the shared semaphore, so a greedy session queues itself rather than the others.
Off by default (`0`); a hosted service should set it to `pool_pages // expected_sessions`.

## Release of resources

- A browser page is released the moment a content-only path has its HTML (auto
  escalation, crawls, plans), so pages are held only while a caller interacts.
- `keep_alive=True` pages get the **idle safety net** (`limits.page_idle_ttl`, default
  600 s): a forgotten page comes back to the pool. A numeric `keep_alive` is its own TTL.
- Stores are bounded LRUs: `service.max_docs` (shared handles), `max_session_docs` (per
  session), `max_sessions`; sessions expire by `ttl` and are swept on creation.
- Closing a client cancels every outstanding task on its loop in bounded rounds
  (`limits.engine_stop_timeout`), closes the pool and the browser contexts.

## No deadlocks

The pool never holds a permit across a factory failure (permit returned in `finally`); a
release always returns its permit even if the client's close raises; the crawl's step lock
is not held across fetches; a bus handler must not call a sync facade method (the loop
refuses, `RuntimeError`). `tests/test_scale.py` drives 100 concurrent plans through a
4-client pool and asserts every lease came back.

## Resiliency at every external boundary (N17)

| boundary | policy |
|---|---|
| web transport | `Resolve.retry` / `rate` / `proxy` / `antibot` (+ the `X-WebClient-*` headers to the proxy service); the resolve ladder's tiers |
| the remote **service connection** | `ServiceTransport(retry=RetryPolicy)` — transport errors and 429/5xx retried with backoff, honouring `Retry-After`; a client's own `Resolve.retry` rides along (`wc.remote(...)`) |
| the LLM | `LlmClient(max_retries, min_interval, budget)` — 429/5xx/529 with backoff, a client-side rate limit, a spend cap |
| a slow origin | `timeout` per client / per reference; the lab's `/lab/slow` exercises it |

## Horizontal: many webclients

The service is stateless **except sessions**: a `RemoteWebClient` is a server-side
session (its handles, cookies and pages live on the node that created it), so route by
session. The recipe:

1. Run N replicas of the `service` image (or the `browser` image for JS tiers); each is
   one process = one engine = one pool. Size `WEBCLIENT_BROWSER__POOL_PAGES` to the pod's
   memory (Chromium ≈ 100–200 MB per page; a 2 GiB pod ≈ 4–6 pages).
2. Sticky-route on the session id (the `POST /sessions` response; the remote client threads
   it as `plan.session_id`) — a cookie or header hash at the ingress. Session-less calls
   (`/tools/*`, `/execute` without a session) go anywhere.
3. Autoscale on queue depth: `/health` → `resources.pool.waiting` (leases queued) is the
   signal; `ResourceEvent(exhausted)` is the alarm.
4. Traces are files: mount `WEBCLIENT_TRACES_DIR` on shared storage so `/ui` on any
   replica can replay a run from any other.

`deploy/k8s.yaml` is a minimal Deployment + Service + HPA with these settings.

## What we measured (baseline, this machine)

`scripts/profile.py --n 200 --concurrency 20` against the lab: ~50 req/s static on a
4-client http pool, p50 ≈ 100 ms (dominated by the lab's Python server), RSS ≈ 110 MB,
Python heap ≈ 20 MB. The next optimisation targets, in order: the per-fetch lxml parse
(cache is per document already), skeleton rendering on large pages (`collapse=True`),
and browser context creation (reuse a context per session when stealth identity allows).
