# Product website — user stories

*Status: proposed 2026-09-24. Feature A of two (the other is the Playground). Scope: a
proper marketing + documentation site for webclient that also IS the package's torture
test — every claim on the site is backed by a page the client can be pointed at, with a
published expected result.*

## 1. Purpose and the two audiences it serves at once

The website has one job for humans — convince a visitor that webclient is the right way
to get data out of the web, and get them to a first success in minutes — and one job for
the package: be the realistic, adversarial site that tests, demos and docs run against.
Those are not in tension: the more real the site is (a JS-rendered changelog, a paginated
case-study listing, a search form, a login for the playground, a PDF whitepaper, an RSS
feed), the more honestly it exercises the client. **Rule: every feature section on the
site is demonstrated live against the site itself**, and every one of those demonstrations
has a machine-readable expected result the test suite asserts.

The current `webclient.lab` is the seed: 24 bare fixture pages with expected JSON. The
website replaces it with real pages that keep the same contract (`/.lab/index.json`,
`/.lab/<page>.json`), so `tests/test_lab.py` keeps running unchanged against the new site.

## 2. Personas

| Persona | Who they are | What convinces them | What loses them |
|---|---|---|---|
| **Evaluator (Eva)** | a senior engineer with a scraping/agent problem, 5 minutes, skeptical | a runnable answer to "why not Playwright / Firecrawl / a Claude session", a real demo, honest limits | marketing fluff, no code above the fold, "contact sales" |
| **Builder (Ben)** | an agent/LLM developer wiring tools | a tool list with schemas, MCP one-liner, typed errors with remedies, the playground | vague "AI-ready" claims, no error semantics |
| **Data owner (Dana)** | data engineer/analyst who needs datasets, not scripts | the author-once-run-forever story, pagination handled, provenance, a query that survives redeploys | anything that looks like a fragile selector |
| **Operator (Oli)** | platform/infra, must run it in their cluster | self-hosted, container, k8s recipe, observability (traces, /health), no data leaving | SaaS-only, opaque runtime |
| **Stakeholder (Sam)** | non-technical decision maker forwarded the link | one diagram, three outcomes, a case study, price/licence clarity | a wall of API |
| **Contributor (Cal)** | wants to add a signal, a tool, a backing | architecture in one screen, the registries, the lab contract, the gate | tribal knowledge |

## 3. Information architecture

```
/                       Home: hero + the one idea + live proof strip + three paths
/why                    "Why not X": Playwright · Firecrawl-style SaaS · a Claude session — side-by-side, runnable
/features               Index of feature pages (each is a live demo against this site)
  /features/plans       a query is data (blob, wireframe, explain, replay)
  /features/transport   cheapest tier that works (auto escalation, flags with evidence)
  /features/signals     what it detects (generated from the registry) with live examples
  /features/crawl       scored, scoped crawl · locate loop · sitemap/robots
  /features/onboarding  author once, run forever (brief → pipeline → blob)
  /features/traces      record, replay (static / HAR / live), rrweb, the ledger
  /features/tools       one registry: Python · MCP · HTTP; the schemas, live
  /features/scale       pool, fairness, resources, the k8s recipe
/docs                   the mkdocs site (generated references + guides), searchable
/playground             hand-off to Feature B (login-gated; the login page is a fixture)
/lab                    the fixture index (this site's pages as test cases + expected JSON)
/changelog              JS-rendered from /api/changelog (an SPA fixture)
/case-studies           a paginated listing (rel=next + ?page=) of scenario write-ups
/case-studies/<slug>    detail pages (a record-list → detail crawl fixture)
/benchmarks             a large page (the large_document fixture) + the profile numbers
/whitepaper.pdf         the PDF fixture
/feed.xml, /sitemap.xml, /robots.txt
/pricing                licence + self-hosting + support tiers (decision pending)
/login, /account        the login-wall fixture; the playground's auth
```

## 4. Story map

Priority: **P1** must ship for launch · **P2** should · **P3** later. Each story lists the
**fixture(s)** it doubles as (the background test) where relevant.

### Epic A — First impression (Home)

- **A1 (P1)** As Eva, I want the home page to state in one sentence what webclient is and
  show a real fetch → typed rows in under 10 lines, so that I know in 30 seconds whether to
  keep reading. *AC:* code above the fold; the snippet is copy-pastable and runs against
  this site; the result shown is produced live (server-side, cached ≤ 60 s), not a static
  image. *Fixture:* the home page itself is `shop`-like (a product grid with prices).
- **A2 (P1)** As Sam, I want one diagram of the idea (plan → sync/async/remote/lazy) and
  three outcomes (cheaper, deterministic, auditable), so that I can forward it. *AC:* SVG,
  legible on mobile, no jargon in the outcome lines.
- **A3 (P1)** As Eva, I want a "live proof" strip on the home page — the transport tier
  chosen for three of this site's pages (static / auto-escalated / API) with the flags that
  decided it — so that the "cheapest tier that works" claim is demonstrated, not asserted.
  *Fixture:* `spa` (changelog), `feed` (API-backed), a static page.
- **A4 (P1)** As any persona, I want three clearly labelled paths (Evaluate · Build ·
  Operate) so that I land on the page written for me. *AC:* each path is one click and
  keeps the same top nav.
- **A5 (P2)** As Sam, I want a short case study (one real scenario: onboarding a
  paginated dataset) with a before/after, so that the value is concrete.
- **A6 (P3)** As Eva, I want a "try it on your URL" box on the home page that runs `card`
  + `flags` + `patterns` against my URL (rate-limited, SSRF-guarded) and links into the
  playground with the result loaded, so that I get a personal answer immediately.

### Epic B — Why (the objections, answered with running code)

- **B1 (P1)** As Eva, I want Playwright vs webclient side by side for the same task
  (extract a listing; handle a JS page; follow pagination) — the script vs the plan, with
  both runnable — so that "declarative plan" means something. *AC:* both columns are real
  code; the plan column links to the wireframe; the run output is identical rows.
- **B2 (P1)** As Eva, I want "why not a Claude session per run" answered with the cost
  and determinism table and a live re-run counter (the same blob run 3× → identical rows,
  $0), so that the author-once story lands. *Fixture:* `onboarding` demo against the
  case-studies listing.
- **B3 (P2)** As Ben, I want "why not a scraping SaaS" answered honestly: what they do
  better (managed proxies, scale) and what we do differently (self-hosted, one plan IR,
  typed errors, replay), so that I trust the page. *AC:* names competitors' strengths.
- **B4 (P2)** As Eva, I want the "what it does NOT do" list (no CAPTCHA solving, no
  managed proxy network, browser-tier limits) so that I don't discover them later.

### Epic C — Feature pages (each is a live demo of this site)

Every feature page has the same skeleton: the claim in one line · a live demo against a
page of this site (input on the left, output on the right, the code that produced it
below) · "what to read next". The live demos run server-side through the service's
`/tools/*` and `/execute` endpoints against `localhost` (the site itself), cached.

- **C1 Plans (P1)** As Dana, I want to see a query as a blob, its wireframe and its
  `explain` tree, and run it against the case-studies listing, so that I understand
  "the query is data". *Fixture:* `paginated` (case studies), `shop` (home grid).
- **C2 Transport (P1)** As Eva, I want to fetch the changelog with `browser="auto"` and
  see the escalation trail + the flags and their evidence, so that I trust the ladder.
  *Fixture:* `spa` (changelog rendered from `/api/changelog`), `feed`.
- **C3 Signals (P1)** As Ben, I want the signals catalogue rendered from the registry
  (12 flags, 32 detectors) with a live example page for each flag on this site, so that I
  know exactly what is detected and how. *Fixture:* `login`, `antibot` (a demo
  interstitial at `/blocked`), `tabs` (docs page with tabs), `forms` (docs search),
  `shadow` (a web-component widget), `iframe` (an embedded video card), `large`
  (benchmarks), `pagination`.
- **C4 Crawl (P1)** As Dana, I want to watch a crawl of this site (frontier scored,
  resources dropped, robots honoured) and a locate loop that stops at the page I described,
  so that I see steering and stopping. *Fixture:* `sitemap`, `redirect` (old URLs),
  `errors` (a 404 page, a `/status/500`), `slow`.
- **C5 Onboarding (P1)** As Dana, I want to type a one-line brief ("the case studies with
  their dates") and watch the pipeline stages (search skipped — seeded with this site,
  crawl, select, evaluate, confirm, source, query) produce a tested blob, then re-run it
  without a model, so that "author once" is shown end to end. *AC:* interactive confirm
  gate works in the browser; the LLM is a real model when a key is configured server-side,
  else the scripted stub with a visible "demo model" badge.
- **C6 Traces (P1)** As Oli, I want to open a trace recorded by the C5 run — timeline,
  snapshots, the rrweb replay of the confirm step, the ledger, the HARs — and re-run its
  plan offline from the HAR, so that observability and replay are seen, not described.
- **C7 Tools (P1)** As Ben, I want the tool list with input schemas (from `GET /tools`),
  a "call it" form per tool, the MCP config snippet and the Python one-liner, so that I can
  wire an agent in one minute. *AC:* the forms call the live service against this site.
- **C8 Scale (P2)** As Oli, I want `/health` of the site's own service shown live (pool,
  resources), the profile numbers, and the k8s recipe, so that I can size a deployment.
- **C9 Errors (P2)** As Ben, I want the error catalogue (generated) with a "trigger it"
  button per code (404 page, login wall, blocked page, bad selector) showing the problem
  details returned, so that I see the remedy vocabulary in action.
- **C10 Scripts (P3)** As Cal, I want the named page scripts listed with what each
  detector needs, and a demo registering a custom script, so that extension is obvious.

### Epic D — Docs

- **D1 (P1)** As Ben, I want the docs (mkdocs) integrated under `/docs` with site-wide
  search and the same nav/theme, so that reference and marketing are one site.
- **D2 (P1)** As Eva, I want a 5-minute quickstart that ends with rows from this site and
  a second one that ends with an MCP tool call, so that both paths have a first success.
- **D3 (P2)** As Cal, I want "architecture in one screen", the registries (tools /
  signals / patterns / scripts / errors) and "how to add one of each", so that I can
  contribute without a call. *Fixture:* the docs search form (`forms`), tabs (`tabs`).
- **D4 (P2)** As Dana, I want a cookbook of recipes (paginated listing → rows; login
  behind a session; SPA via its API; crawl a section; schedule a blob), each runnable
  against this site.

### Epic E — The site as the lab (the background test contract)

- **E1 (P1)** As the test suite, I want `/.lab/index.json` listing every page that is a
  fixture (`name`, `path`, `feature`, `browser`) and `/.lab/<name>.json` with its expected
  result, so that `tests/test_lab.py` runs unchanged against the real site. *AC:* every
  current lab fixture has a real-page counterpart; no expected value is hand-copied — the
  site's data source (the case studies, the changelog JSON, the product grid) generates
  both the page and the expected JSON.
- **E2 (P1)** As the suite, I want the site to run offline in-process for tests (a
  `LabServer`-style launcher of the built site + its APIs on a random port) and in a
  container for the demos/docs, so that both stay one artefact.
- **E3 (P1)** As the suite, I want deliberately adversarial paths: `/blocked` (a
  Cloudflare-style interstitial with the vendor headers), `/status/{code}`, `/slow?delay=`,
  `/old/*` → `/case-studies/*` redirect chain, a gzip'd page, a mislabeled content type, a
  cookie-gated `/account`, so that the resiliency code is exercised by real routes.
- **E4 (P2)** As the suite, I want the interactive pages (the playground login, the
  changelog filter, the case-study "load more") to be the `app` / `scroll` fixtures, so
  that live interaction, recording and rrweb are tested on real UI.
- **E5 (P2)** As Cal, I want a CI job that builds the site, serves it, runs the lab suite
  and the demos against it, and fails on any drifted expected value, so that the site can
  never contradict the product.
- **E6 (P3)** As Oli, I want the site's own service (the one powering the live demos) to
  run with the SSRF guard, a token for write endpoints, rate limits per IP and traces
  written to a rolling directory, so that the public demo is safe.

### Epic F — Conversion and trust

- **F1 (P1)** As Sam, I want the licence and "self-hosted, your data never leaves" stated
  plainly on every path, so that procurement has no questions. *(Decision: licence.)*
- **F2 (P2)** As Eva, I want a status/benchmarks page with the profile numbers and the
  test count, updated by CI, so that quality claims are verifiable.
- **F3 (P2)** As Sam, I want pricing/support tiers (or an explicit "open source, support
  available"), so that I know what it costs. *(Decision.)*
- **F4 (P3)** As any persona, I want a newsletter/changelog subscription (the RSS feed +
  an email form — the `forms` fixture), so that I can follow releases.
- **F5 (P3)** As Eva, I want GitHub stars/issues and the roadmap linked, so that I see
  it's alive.

## 5. Design requirements

- **Tone:** engineer-to-engineer; every claim adjacent to the code that proves it; no
  stock imagery. Screens are the product (the playground, the trace viewer, the wireframe).
- **Live, not screenshots:** feature demos execute against the site through the service
  (cached, rate-limited); a "last run: 12 s ago · 0.4 s" stamp on each.
- **Performance:** static-first (SSG); the live widgets hydrate lazily; LCP < 1.5 s on the
  home page; no third-party scripts except optional analytics.
- **Accessibility:** WCAG AA; all demos keyboard-operable; code blocks with copy; dark
  mode.
- **Mobile:** every page readable; demos degrade to "run on desktop" with the output
  shown.
- **Consistency with the playground:** shared design tokens (colour, type, spacing) and
  the same trace/wireframe components — the site embeds the playground's components in
  read-only mode.

## 6. Proposed stack (your call — installs are fine)

- **Astro** (static-first, islands for the live widgets, MDX for feature pages, built-in
  content collections for case studies) + **React islands** sharing components with the
  Playground; **Tailwind** with shared tokens; **Pagefind** for docs search; mkdocs output
  mounted under `/docs` (or migrate docs to Astro Starlight later).
- Live demos call the webclient **service** (FastAPI) deployed next to the site; the
  site's dynamic routes (`/api/changelog`, `/status/{code}`, `/slow`, `/blocked`, login)
  are a small FastAPI app (the current `webclient.lab` router, grown) that also serves
  the built static site — one container.
- Repo layout: `site/` (Astro project), `webclient/lab/` becomes the site's dynamic
  routes + the fixture contract; `deploy/` gains the site.

## 7. Open decisions

1. Product name/brand and domain (the package is `webclient`; is that the product name?).
2. Licence and pricing/support model (F1, F3).
3. Whether the public site runs a real LLM for the onboarding demo (C5) or the scripted
   model only.
4. Astro (proposed) vs Next.js; Tailwind vs a component library.
5. Docs: keep mkdocs under `/docs` or migrate to the site's framework.
