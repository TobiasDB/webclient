# web

A web client for turning messy, defended, JavaScript-heavy websites into clean structured data —
and for doing it *politely and only as hard as a given site forces you to*.

It's built as a stack of small, independent layers (`web.fetch`, `web.parse`, `web.resolve`,
`web.crawl`, `web.dsl`, `web.onboard`). Each one is useful on its own and depends only on the ones
below it, so you can reach for a single layer (just fetch a page, just parse some HTML) or drive the
whole thing (give it a goal and a company and let it find and extract the dataset for you).

```python
from web.dsl import wq

rows = await (
    wq.reference("https://example.com/investors/events")
      .resolve()
      .select_all("article.event")
      .extract(
          title=wq.doc.select(".title").attr("text"),
          date=wq.doc.select("time").attr("datetime").datetime(),
          webcast=wq.doc.select("a.webcast").attr("href"),
      )
      .acollect()
)
```

That's the shape of it: you *record* a plan against a typed surface, and it runs — synchronously,
asynchronously, or serialized and shipped to a remote worker. Same plan, every mode.

## How it's put together

Each layer is its own package under [`webclient/`](webclient/), with its own tests and gate.

| Layer | Package | What it does |
|-------|---------|--------------|
| **fetch** | `web.fetch` | Transport only: `Request → Snapshot`. HTTP, TLS-impersonating HTTP, and real browsers, behind one interface. Never parses, never raises for a bad response. |
| **parse** | `web.parse` | Bytes → `Document`. Sniff the kind, decode, select, extract records, render markdown, read JSON. Pure and offline. |
| **resolve** | `web.resolve` | The opinionated face: wraps fetch in a policy chain — retry, rate-limit, and the **anti-bot escalation ladder** (below). Detects *why* a page is blocked and climbs accordingly. |
| **crawl** | `web.crawl` | Bounded, goal-directed crawling over a resolver — robots/sitemap aware, with a pluggable frontier. |
| **dsl** | `web.dsl` | The `wq` query language: record a lazy plan, run it four ways (sync / async / service-blob / remote). This is the surface most code touches. |
| **onboard** | `web.onboard` | The capstone: *goal + company → dataset*. Locate the right source, then have a model author the extraction query. |

## The anti-bot problem, and our strategy

Modern sites don't just serve you HTML. Before you see a byte, a defender may have already
fingerprinted your TLS handshake, checked your IP's reputation, and decided whether to hand you the
page, a JavaScript challenge, or a CAPTCHA. There are two ladders here, and **detection is cheap
while evasion is expensive** — so both sides climb only as far as they're forced to.

Our strategy is to climb the *realness* ladder from the cheapest possible request up to a genuine
Chrome on a residential IP, adding one layer of authenticity at a time, and **only as far as a
given site actually pushes back**. The escalation is *reason-aware* (it reads the specific block
signal and picks the rung that answers it) and *domain-sticky* (once a host needed a browser, we
start there next time instead of re-climbing).

```
     WHAT THE SITE CHECKS                       HOW WE ANSWER IT
     (cheap → expensive)                        (cheap → real)

  ┌───────────────────────────┐            ┌──────────────────────────────────────────┐
  │ IP / ASN reputation        │──────────▶ │  + Proxy   residential / mobile IP        │  ◀ strongest
  ├───────────────────────────┤            ├──────────────────────────────────────────┤
  │ Behaviour, CAPTCHA         │            │  REAL_CHROME   genuine installed Chrome    │
  ├───────────────────────────┤            │                (real build / GPU / CDM)    │
  │ Browser fingerprint        │──────────▶ │  HEADED_BROWSER headed window, no          │
  │ (canvas / WebGL / JS)      │            │                 headless tells (Xvfb)      │
  ├───────────────────────────┤            │  BROWSER   headless stealth Chromium —     │
  │ JavaScript / PoW challenge │──────────▶ │            runs JS, real fingerprint       │
  ├───────────────────────────┤            ├──────────────────────────────────────────┤
  │ TLS / HTTP-2 fingerprint   │──────────▶ │  BASIC   real Chrome TLS/HTTP-2            │  ◀ cheapest
  │ (JA3/JA4, read first)      │            │          (curl_cffi), no JS                │
  └───────────────────────────┘            └──────────────────────────────────────────┘
```

A few things worth knowing about how those rungs are chosen:

- **`BASIC` is not plain `httpx`.** It presents a real Chrome's TLS + HTTP/2 fingerprint (via
  `curl_cffi`) at plain-HTTP cost — because a stock client's JA3/JA4 is flagged before a byte of
  content is read. Plain `httpx` is only the fallback when that library isn't installed.
- **The first real jump is `BROWSER`** — a leak-patched (patchright) headless Chromium that actually
  runs JavaScript and presents a full, coherent browser fingerprint (canvas, WebGL, navigator).
- **`HEADED_BROWSER` and `REAL_CHROME`** shed the remaining "this is automation" tells — a real
  on-screen window, then the genuine installed Chrome binary rather than the bundled test build.
- **Proxies** are the answer to IP/ASN reputation, orthogonal to all of the above.

Each rung earns its place: it closes a *distinct* detection layer at a *distinct* cost. The full
write-up — the papers, the provider tiers, the fingerprinting surface — lives in
[`webclient/ANTI-BOT.md`](webclient/ANTI-BOT.md).

## Onboarding: from a goal to a dataset

`web.onboard` is the part that ties it all together. Give it a brief (a goal + a schema, plus
optional per-stage hints) and a company, and it runs two phases:

1. **Locate** — search + crawl for the company's *own* source (not a third-party aggregator), score
   the candidates, and review the winner. It also works out *how the data actually loads* — if the
   records aren't in the static HTML, it renders in a browser and, when there's a JSON data-API
   behind the page, prefers that.
2. **Author** — a model writes a `wq` extraction query, in a loop that *checks* the data is really
   there, *reviews* each sample against the brief, *repairs* a query that fails or comes back empty,
   and, when a dataset spans two pages (an "upcoming" list and a separate "archived" one), authors
   both and concatenates them.

The brief drives each stage in plain language — what to look for while crawling, patterns to suggest
while authoring, how strict to be while reviewing — so a dataset-specific rule (say, "the events
list must include upcoming ones, not only past") lives in the brief, never hardcoded in the pipeline.

## Layout & the gate

```
webclient/
  fetch/  parse/  resolve/  crawl/  dsl/  onboard/   # one package each: web/<pkg>/ + tests/
  demo.py                                            # an offline tour of the whole stack
  ANTI-BOT.md  ARCHITECTURE.md  REVIEW-CHECKLIST.md
pyproject.toml                                       # authoritative black / isort / pyright config
```

Every package holds the same bar green — `mypy --strict`, `pyright`, `pytest`, and `isort`/`black`
(line length 100). `webclient/demo.py` runs the whole stack offline against a local server.
