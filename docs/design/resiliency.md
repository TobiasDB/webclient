# Resiliency-as-policy — design

*Status: design (research-driven). Build base for the resiliency feature. Planning
only — no code yet. Composes with the Summary `probe` facet (its read-side).*

## Update (2026-09-14) — confirmed decisions & current architecture

**Decisions locked:**
- **API:** each concern kwarg accepts **`Policy | "auto" | None`** — a Policy object
  to configure manually, `"auto"` for cheapest-first / escalate-on-evidence, or
  omitted = off/inherit. Bare `resolve()` / `fetch()` stays **static-only**;
  escalation is opt-in (like today's retries).
- **Home:** the five Policy models + the `Resolve` bundle are pydantic value models,
  so they follow the per-package `models.py` convention → **`core/reference/models.py`**
  (a `Reference` is "a resolvable request spec"; it sits low in the dep graph, so the
  client/session import `Resolve` for their default field and a `Reference` can carry a
  `resolve: Resolve` override with no cycle).

**Re-grounding on the current code (names/anchors have moved):**
- Cores are `WebClient` / `Document` / `Reference` / `Session` (no `Core` suffix), each
  implementing a generated `I<Core>` interface (fields + ops) in its `models.py`.
  Policies are **Core Fields** → a default `resolve: Resolve` on `IWebClient` (optional
  on `IReference`); **no new surface classes** (the core IS the surface).
- `WebClient.afetch(ref, *, optional, browser)` is still the single transport entry —
  the ladder + `_aresolve_laddered(ref, policy)` live here (machinery, not a backing).
  `Reference.resolve(...)` (ResolveBacking) and `WebClient.fetch` thread the per-call
  policy in; **every higher verb — `search`, and `crawl` / `sitemap` — funnels through
  `afetch`, so policies apply to them for free.**
- **Read-side is already built:** the `Probe` facet exists (`core/document/models.py`).
  P0 adds `Document._probe: ProbeRecord | None` the ladder writes; the facet reads it.
  Composes with the "top-of-class Summary" work.
- Detection stays pure (`webclient/resiliency/detect.py`) so the `remote` core-swap runs
  the identical ladder server-side; the crawl/service tiers inherit it unchanged.

**P0 (first commit, zero behaviour change):** the 5 models + `Resolve` in
`core/reference/models.py`; map today's `retries` / `retry_backoff` / `min_interval` /
`proxy` onto `RetryPolicy` / `RatePolicy` / `ProxyPolicy`; add the `resolve=` default
field + per-call kwargs (carried, not yet acted on); add `Document._probe` + populate
the `Probe` facet trivially (`was_browser_required` from the browser flag).

The caller writes, per concern:

```python
retry      = Policy(...) | "auto"
rate_limit = Policy(...) | "auto"
proxy      = Policy(...) | "auto"
anti_bot   = Policy(...) | "auto"
browser    = Policy(...) | "auto"
```

used as `ref.resolve(browser="auto", antibot="auto", proxy="auto")` and as
client/session defaults. `"auto"` means, per concern: **cheapest path first,
escalate on evidence** (Firecrawl's `proxy=auto` model).

## How it maps onto the architecture

Anchors read from source:

- **`WebClientCore.afetch(ref, *, optional, browser)`** is the single transport
  entry: SSRF guard → browser branch (`_alive`) → `_pace` (politeness) →
  `_afetch_once` → a retry loop honouring `Retry-After`. `retries`,
  `retry_backoff`, `min_interval`, `block_private_hosts` are Core Fields.
  **`afetch` is machinery, not a backing op — the ladder lives here.**
- **Backings** serve ops via `provides`/`gate`/`applies` reached through
  `dispatch`. Policies are *data*, not a new medium — no new surface classes.
- **`ReferenceCore.resolve(browser=False)`** calls `target.afetch(...)` — where
  the per-call policy kwargs enter.
- **`HTTPXFactory`/`HTTPXClient`** bake `proxy=` at construction — a rotating
  per-request pool needs this to become per-request (mount a transport).
- **Remote is a core-swap**: the ladder must run server-side too, so detection
  must be pure.

## 1. The `Policy` types

Frozen pydantic models, one per concern; `"auto"` is a per-concern sentinel
(never a magic dict). They **generalise fields that already exist**.

```python
class RetryPolicy(BaseModel, frozen=True):
    max: int = 2
    backoff: Literal["exp","const"] = "exp"
    base: float = 0.2
    on_statuses: frozenset[int] = frozenset({429,500,502,503,504})
    on_transport: bool = True
    respect_retry_after: bool = True          # already in afetch
    # auto = 3x exp, honour Retry-After

class RatePolicy(BaseModel, frozen=True):
    rps: float | None = None
    per: Literal["host","global"] = "host"
    concurrency: int | None = None
    burst: int = 1
    adaptive: bool = False
    # auto = adaptive AutoThrottle per host, target_concurrency=1  (generalises min_interval/_pace)

class ProxyPolicy(BaseModel, frozen=True):
    pool: str | list[str] | None = None       # provider gateway OR upstream URLs
    geo: str | None = None                     # cc / city / asn
    sticky: Literal["session","host","none"] = "session"
    rotate_on: frozenset = frozenset({403,429,"session_error"})
    ttl: float = 600
    # auto = default pool, sticky-by-session, rotate-on-block

class AntiBotPolicy(BaseModel, frozen=True):
    level: Literal["off","stealth","max"] = "off"
    captcha: Literal["off","auto"] = "off"
    # auto = stealth + captcha auto, engaged only when a challenge is DETECTED

class BrowserPolicy(BaseModel, frozen=True):
    engine: str = "chromium"
    stealth: bool = False
    wait_for: str | None = None
    when: Literal["never","auto","always"] = "never"
    # auto = render only when static is empty / JS-gated / blocked (Crawlee adaptive)
```

Grouped into a `Resolve` bundle (`retry/rate_limit/proxy/anti_bot/browser`).
`WebClientCore`/`WebSessionCore` hold a default `Resolve` Core Field; per-call
kwargs shallow-merge per concern.

## 2. Plugging in ("behaviour in backings; remote is a core-swap; same surface")

- **Policies are data** — add `resolve_policy: Resolve` Core Fields (defaults) and
  thread an optional per-call `Resolve` into `afetch`/`resolve`. No new surfaces.
- **The ladder lives in the client-core transport**, next to `afetch` (already the
  single machinery entry). Add `_aresolve_laddered(ref, policy)` that `afetch`
  delegates to when a policy requests escalation; each tier reuses `_afetch_once`
  (static) and `_alive` (browser). `RemoteWebClientCore` POSTs the plan and the
  **service** runs the identical ladder → same surface, core-swap unchanged.
- **Detection is pure functions** in `webclient/resiliency/detect.py` —
  `classify(status, headers, cookies, body) -> Signals`. Pure so local and remote
  agree; reads a raw response, not a dispatch target (a backing would be the wrong
  shape).
- **Read side = a new `DocumentCore.probe: ProbeRecord | None` field**, written by
  the ladder as it escalates; the Summary `probe` facet reads it directly.

```python
class ProbeRecord(BaseModel):
    was_browser_required: bool
    was_proxy_required: bool
    anti_bot: str | None          # cloudflare|datadome|perimeterx|akamai|kasada|awswaf|incapsula|None
    js_required: bool
    paywall: bool
    login_wall: bool
    render_blocked: bool
    escalation: list[str]         # tiers taken, e.g. ["static","proxy","browser","browser+stealth"]
    reason: str                   # final trigger, e.g. "datadome-403"
    attempts: int
    final_tier: str
```

## 3. The escalation ladder

Cheapest first (Firecrawl auto / Browserless "stealth → +residential → +solve"):

```
T0 static httpx  →  T1 static+proxy  →  T2 browser  →  T3 browser+stealth(+captcha)
```

`retry` and `rate_limit` are **orthogonal** — applied *within* each tier. Each
attempt appends to `ProbeRecord.escalation`.

**Detection → trigger → probe field** (body first, never the `Server` header):

| Trigger | Response signals | probe field | Ladder action |
|---|---|---|---|
| Empty / JS-gated | body far shorter than headers imply; empty SPA root (`<div id="root">`); content only in `<noscript>`; title but no text | `js_required` | → T2 browser |
| Hard block / geo | status ∈ {401,403,429} | (feeds `was_proxy_required`) | → T1 proxy (rotate), then T2/T3 |
| **Cloudflare** | `cf-ray`, `cf-mitigated: challenge`, cookies `__cf_bm`/`cf_clearance`, "Just a moment…"/"Checking your browser"; 403/503/429 | `anti_bot="cloudflare"` | → T2/T3 |
| **DataDome** | header `x-datadome`, `Set-Cookie: datadome=`, `dd` script / captcha page; 403 | `anti_bot="datadome"` | → T3 |
| **PerimeterX/HUMAN** | `Set-Cookie: _px*`/`_pxhd`, `_pxAppId`/`px-captcha`; 4xx | `anti_bot="perimeterx"` | → T3 |
| **Akamai** | `Set-Cookie: _abck`/`ak_bmsc`, `akamai-grn`, `Server: AkamaiGHost`; 403 | `anti_bot="akamai"` | → T3 |
| **Kasada** | bare 429 empty body, `x-kpsdk-*` | `anti_bot="kasada"` | → T3 |
| **AWS WAF** | `x-amzn-waf-*`, `aws-waf-token`, branded captcha | `anti_bot="awswaf"` | → T3 |
| **Incapsula/Imperva** | `Set-Cookie: visid_incap`/`incap_ses`, `X-Iinfo`, "Incapsula incident" | `anti_bot="incapsula"` | → T3 |
| CAPTCHA present | `g-recaptcha`/`h-captcha`/`cf-turnstile`/`px-captcha` | sets `anti_bot` + captcha | → T3 browser+stealth+solve |
| Paywall | status 402; JSON-LD `isAccessibleForFree:false`; "subscribe to continue" | `paywall` | **record, do NOT escalate** |
| Login wall | redirect to `/login`, `input[type=password]`, 401 | `login_wall` | **record, do NOT escalate** |
| Still blocked at T3 | browser tier returns challenge/blank | `render_blocked` | terminal failure |

`was_browser_required` = ladder reached ≥T2; `was_proxy_required` = ≥T1 with
proxy. Terminal outcomes (paywall/login_wall/render_blocked) stop the ladder so we
don't burn expensive tiers on what they can't fix.

## 4. The ProxyService

**Control-header protocol** — `X-Policy-*` set on the outgoing request, stripped
before egress; results echoed on the response:

```
Request:
  X-Policy-Retry:      max=3;backoff=exp:0.2;on=429,503,tx
  X-Policy-Rate:       rps=1;per=host;concurrency=1;adaptive=1
  X-Policy-Proxy-Pool: name=residential;geo=us;sticky=session:<key>;rotate=on-block
  X-Policy-AntiBot:    level=stealth;captcha=auto
  X-Policy-Tier:       static|proxy|browser|stealth
Response:
  X-Policy-Used-Proxy: <exit-id>
  X-Policy-Used-Tier:  proxy
  X-Policy-Detected:   datadome
  X-Policy-Escalate:   browser        # a hint the client ladder can act on
```

**Proxy-pool rotation:** the pool is either a provider gateway (encode geo/session
in the username, the Bright Data/Oxylabs shape) or a list of upstream URLs.
**Sticky-by-default** — bind a chosen exit to the `sticky` key for `ttl`; **rotate
only on a block/session-error**. Rotations are a **separate budget** from retries
(Crawlee `max_session_rotations` vs `max_request_retries`).

**Placement (two-part, Firecrawl basic-vs-managed split):**

- **Cheap tiers (retry/rate/proxy): in-process, an httpx custom transport mount.**
  `ProxyTransport(httpx.AsyncBaseTransport)` behind the existing `ClientPool`:
  reads `X-Policy-*`, runs a per-host async token bucket (rate), selects/rotates a
  proxy, does same-tier transport retry, then dispatches to the real proxied
  transport. **Zero new threads.** Requires `HTTPXClient`/`HTTPXFactory` to stop
  passing `proxy=` at construction and mount `ProxyTransport` (exit chosen
  per-request). *In-process over a sidecar: these are request-routing/timing
  decisions over per-host state the engine already owns.*
- **Expensive tiers (browser/stealth/anti-bot/captcha): a sidecar/remote service**
  (Browserless-shaped, over CDP/HTTP), reached through the **existing
  `RemoteWebClientCore` core-swap** — a heavy, separately-scaled resource that
  should not run in-process.

**Compose:** same-tier concerns (transport retry, rate, proxy rotation) live in the
ProxyService transport; cross-tier escalation retry (static→proxy→browser) lives in
the client ladder — Crawlee's `max_request_retries` vs `max_session_rotations`
separation, so a flurry of rotations can't exhaust the escalation budget.

## 5. Phased build order (least new surface first)

- **P0** — Policy types + plumbing, no behaviour change. Add the five models +
  `Resolve` bundle; accept the kwargs on `resolve`/`fetch`/client/session; map
  today's `retries`/`retry_backoff`/`min_interval`/`proxy` onto them. Add
  `probe: ProbeRecord | None` + the `probe` summary facet, populated trivially from
  the existing `browser` flag.
- **P1** — Pure detection module; populate `ProbeRecord` on every resolve recording
  *what happened / what would escalate* — **still no escalation** (observe first).
- **P2** — Ladder static→browser (the Crawlee adaptive core). Wire `browser="auto"`.
- **P3** — ProxyService in-process transport + control headers + sticky rotation +
  adaptive rate. Wire `proxy="auto"`, `rate_limit="auto"`. Includes the
  per-request-proxy `HTTPXClient` change.
- **P4** — Stealth + anti-bot tier via sidecar/remote browser + captcha. Wire
  `anti_bot="auto"`.
- **P5 (optional)** — per-host rendering-type predictor (Crawlee) to learn which
  tier to *start* at per host.

## Open decisions

1. **Default aggressiveness** — recommend bare `resolve()` stays static-only
   (escalation opt-in via `"auto"`), consistent with today's opt-in retries.
2. **Cost/budget cap** — a per-resolve max-tier or credit ceiling?
3. **Captcha source** — built-in browser solve (in the sidecar) vs an external API?
4. **Proxy abstraction** — BYO URL pool first, provider adapters as a follow-up.
5. **Probe over the wire** — serialize `ProbeRecord` across the remote core (yes).
6. **Rate reconciliation** — `RatePolicy.concurrency` vs `ClientPool` limits and
   `_pace`/`min_interval`: new per-host token bucket layered on the pool, or a
   re-parameterisation of `_pace`?
7. **Predictor persistence** (P5) — in-memory per-host only, or persisted?

## Sources
- Crawlee AdaptivePlaywrightCrawler / predictor: <https://crawlee.dev/python/docs/guides/adaptive-playwright-crawler>; error handling & session rotation: <https://crawlee.dev/python/docs/guides/error-handling>, <https://crawlee.dev/python/docs/guides/session-management>
- Scrapy AutoThrottle: <https://docs.scrapy.org/en/latest/topics/autothrottle.html>
- Browserless proxies/stealth/captcha: <https://www.browserless.io/blog/residential-proxies-web-automation-browserless>, <https://docs.browserless.io/baas/bot-detection/captchas>
- Bright Data proxy config: <https://docs.brightdata.com/proxy-networks/config-options>; Oxylabs Web Unblocker: <https://developers.oxylabs.io/products/web-unblocker/migration-guides/from-bright-data-web-unlocker>
- Anti-bot detection markers: <https://scrapfly.io/blog/posts/how-to-bypass-anti-bot-protection>, <https://scrapeops.io/web-scraping-playbook/403-forbidden-error-web-scraping/>
- Firecrawl proxy=auto: <https://docs.firecrawl.dev/api-reference/endpoint/scrape>
