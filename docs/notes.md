# Engineering notes / deferred work

Short design notes for changes that are worth doing but too large/risky for an
autonomous, single-commit polishing pass. Each should become a proper task with
tests when picked up.

## True streaming (`astream` / `fan_out`)

**Status:** claims corrected to match reality (2026-09-14); real incremental
streaming still to do.

**Current behaviour.** `WebClient.execute(..., stream=True)` and the async
`_astream`/`_stream` bridges *do* hand rows out one at a time, but only *after*
the whole plan has finished computing: `executor.astream` does
`result = await aevaluate(...)` and then iterates. `fan_out` likewise gathers all
results (in input order) before returning. So delivery is incremental;
computation is not. The docstrings/comments used to say rows are yielded "as they
complete", which was misleading -- now corrected.

**What true streaming needs.**
- A streaming variant of the Collection fan-out that yields each element's result
  as its task finishes (think `asyncio.as_completed`), rather than
  `fan_out` filling an ordered list and returning it whole.
- `astream` (and the terminal `project()` path) to consume that as an async
  generator and yield rows to the caller as they arrive.
- Decide ordering semantics: true streaming implies **completion order**, not
  input order, for the streamed rows. That is a behaviour change and needs a
  test that asserts rows arrive incrementally (e.g. a slow + fast element where
  the fast row is delivered first) plus back-pressure via the existing bounded
  queue in `EngineLoop.stream`.
- Preserve cancellation/error semantics: a failing element should still cancel
  siblings and surface the first error (as `fan_out` does today).

**Why deferred.** It reshapes the core evaluator that all ~150 tests depend on;
worth doing deliberately with the ordering-change and back-pressure tests, not in
a drive-by pass.

## Session store cap eviction (service)

`create_app(max_sessions=...)` now sweeps expired/closed sessions and rejects new
ones past the cap with HTTP 429. It deliberately does **not** evict *live*
sessions to make room (that would silently break a client mid-use). If a
hard-bound-with-eviction policy is ever wanted, close-and-evict the oldest and
document that a client's next op may 404.

## SSRF / host safety boundary (opt-in, implemented 2026-09-14)

`WebClientCore.block_private_hosts` (default `False`) is the opt-in SSRF guard.
When set, `afetch` calls `_host_blocked(ref)` before any transport: an IP literal
or a resolved hostname that is loopback / private / link-local / reserved /
multicast / unspecified, or `localhost`, is refused with a `BlockedHost`
`WebError` (raising unless `optional`). It resolves names (via the loop's
`getaddrinfo`) so a public name pointing at an internal IP is caught. Default-off
keeps the test-suite's `127.0.0.1` fetches working; tests cover both modes.

**Remaining work.**
- TOCTOU: resolution happens in the guard, but the actual connect re-resolves --
  a determined DNS-rebinding attacker could differ. A pinned-IP connect (resolve
  once, connect to that IP with the Host header preserved) would close this.
- Wire it through the service/remote tier: `create_app(...)` should be able to
  construct its `WebClient(block_private_hosts=True)` so a hosted server is
  protected by default; the browser path (`_alive`) is covered by the same
  `afetch` guard, but a defence-in-depth check at navigation would be prudent.
