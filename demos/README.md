# webclient — demos

A guided tour of what webclient is and **why it exists**. Every demo runs fully offline
(it serves its own tiny site), so you can run the whole thing on a plane.

```bash
env/bin/python demos/run_all.py            # the HTTP-only demos (fast, no browser)
env/bin/python demos/02_auto_transport.py  # any single demo
env/bin/python demos/run_all.py --browser  # include the chromium demos (02, 08, 09)
```

Each file starts with a one-paragraph **“THE POINT.”** Read those first.

---

## What webclient is

A **declarative data-extraction layer for the web.** You describe the data you want as a
typed, serialisable *plan*; webclient decides how to fetch it (cheapest transport that
works), extracts it, and hands back typed rows. On top of that sits an **LLM-driven
onboarding pipeline** that turns a page into a validated, reusable query — *once* — so it
can run forever with no model in the loop.

## The two questions a skeptic asks

### “Why not just use Playwright?”

Playwright is a **browser-automation library**. webclient is a **data layer** that uses a
browser only when it has to. Different altitude.

| | Playwright | webclient |
|---|---|---|
| **The query** | imperative script (code you keep + run) | declarative plan (**data** you store, ship, diff) — *demo 01, 04* |
| **Transport** | always launches a full browser | HTTP first; browser **only when flags demand it** — *demo 02* |
| **Analysis** | raw DOM; you detect SPA/login/anti-bot yourself | **signals → flags**, confidence-scored, with evidence — *demo 03* |
| **Crawling** | you write the frontier/dedup/scope/scoring | first-class **scored, scoped crawl** — *demo 05* |
| **Dispatch** | local browser | one API across **sync / async / remote** (a plan ships to a service) |
| **Provenance** | none | **XHR→DOM correlation** — which request produced this data — *demo 08* |

The plan is the moat: because a query is *data*, it is portable, versionable, reviewable,
and runnable without the code that built it. A Playwright script is none of those.

### “Why not just onboard with a Claude session + the web-search tool?”

Because a chat session **re-scrapes and re-reasons on every run** — slow, non-deterministic,
costly, and a black box. webclient uses an LLM **once**, at authoring time, to produce a
**validated, self-contained query blob**. After that, the blob runs with **no LLM**:

| | Claude session per run | webclient onboarding |
|---|---|---|
| **Determinism** | drifts run to run | identical rows every time — *demo 07* |
| **Cost** | pays the model on every run | model paid **once**; every run after is free |
| **Scale** | one conversation at a time | one brief → author N companies → N blobs → typed rows |
| **Auditability** | a chat transcript | a reviewable query + recorded flags/evidence |
| **Why it’s accurate** | dumps raw HTML at the model | a **skeleton** that marks the dataset, where values live, and provenance — *demo 06* |

The onboarding pipeline is not “Claude with extra steps” — it *compiles* a page into a
cheap, deterministic program.

---

## The demos

| # | File | Shows |
|---|---|---|
| 01 | `01_declarative_query.py` | a query is data, not a script (vs Playwright, side by side) |
| 02 | `02_auto_transport.py` | HTTP first; a browser **only** for the JS-gated page 🖥️ |
| 03 | `03_signals.py` | login / SPA / XHR-API detection as confidence-scored flags |
| 04 | `04_portable_plans.py` | blob round-trip + `explain()` tree + HTML `wireframe()` |
| 05 | `05_crawl.py` | scored, scoped, deduped traversal with lean page cards |
| 06 | `06_llm_skeleton.py` | the skeleton that makes an LLM write a **correct** selector |
| 07 | `07_onboard_author_once.py` | **author once, run forever** — the headline |
| 08 | `08_correlation.py` | which request produced this data (provenance) 🖥️ |
| 09 | `09_live_sequence.py` | a stateful multi-step flow as one plan 🖥️ |

🖥️ = needs chromium (`env/bin/python -m playwright install chromium`).
`07` authors with a scripted LLM offline; set `ANTHROPIC_API_KEY` to author with a real model.
