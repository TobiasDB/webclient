# Architectural ethos review: fixed pipeline vs. agentic orchestrator

**Scope:** the onboarding pipeline in `webclient/pipelines/onboarding.py` (+ `llm.py`, the
`prompts/`, `skills/lazy-queries.md`, and the `core/` surface it drives).
**Question:** should onboarding stay a hardwired Python pipeline that calls the LLM as a
stateless text function, or invert so the LLM orchestrates an agentic loop over tools and
decides the flow?
**Date:** 2026-09-17 · **Author:** senior-architect review (strategic, not a bug hunt)

---

## Verdict (up front)

**Keep the deterministic pipeline as the spine. Do not invert it into one open-ended LLM
orchestrator. But recognise that the pipeline is already a hybrid, and push agency
*deeper into the two stages that have a capability ceiling* — query authoring first, frontier
navigation second — while keeping search / select / evaluate / the reference+resolve
cascade / the gates deterministic.**

The "hardwired vs. agentic" framing is a false binary. Measured against how Anthropic
frames the choice in *Building Effective Agents* — *workflows* (LLMs and tools orchestrated
through predefined code paths) vs. *agents* (LLMs directing their own process and tool use)
— this codebase is a **workflow with an evaluator–optimizer sub-loop already embedded in
its hardest stage** (`write_query`). The right move is not to throw out the workflow; it is
to make that embedded loop a first-class, tool-using bounded agent and to give the crawl a
little more agency, because those are the only two places where a fixed DAG structurally
caps what onboarding can achieve.

---

## 1. What the pipeline actually does today

Seven stages wired by `onboard_company` → `_onboard_company`:

1. `search_web` — LLM crafts a query, runs `search()`, LLM verifies which hits belong to the
   company (fail-open).
2. `crawl_from_seeds` — a hand-driven crawl where **each round the LLM picks frontier edges**
   (`_pick_edges`) from a Python-filtered, domain-bounded, docs-banned, dedup'd frontier.
3. `select_candidates` — LLM ranks crawled pages into must/should/could tiers (with fail-open
   and a forced-in rule for seeded data documents).
4. `evaluate_candidate(s)` — LLM judges each candidate's skeleton+flags (present? queryable?
   paginated? subset? api-docs?); Python picks the best by a weighted `_candidate_score`.
5. `write_reference` — **pure Python**, deterministic from the eval (`api_endpoint or url`).
6. `write_resolve` — **pure Python**, deterministic from the page's flags
   (spa/shadow_dom/iframe → browser; anti_bot_triggered → proxy/stealth).
7. `write_query` — LLM authors a `wq.doc…` extraction from the skeleton; Python parses it
   through a **sandboxed AST interpreter** (`_eval_query_ast`, not `eval`), tests it against
   the fetched doc, and **retries up to 4× with escalating, diagnostic feedback**.

Plus an opt-in **review** meta-stage (`review_crawl/select/query/failure`) whose verdicts
`_gate()` the run, and a `Budget` that hard-caps USD spend across the whole batch.

### Who decides what — the honest accounting

| Concern | Decided by | Notes |
|---|---|---|
| Control flow / stage order | **Python** | fixed DAG |
| Which company a hit is | LLM | fail-open to Python |
| Which links to expand | **LLM per round** | constrained agent inside a Python loop |
| Which pages are candidates | LLM | fail-open + forced seeds |
| Is the dataset here / queryable | LLM | flags are ground truth and override |
| The reference URL | **Python** | deterministic from eval |
| Fetch policy (browser/proxy/antibot) | **Python** | deterministic from flags |
| The extraction query | LLM | **generate → verify → repair loop** |
| Did the query actually work | **Python verifier** | `_test_query`, `_populated_rows`, `_empty_required_fields`, `_timeliness` |
| Pass/fail gate | LLM judge + **Python override** | e.g. timeliness gate forced deterministically |

Two things stand out. First, **Python owns every decision where a wrong LLM call would be
expensive, unreproducible, or unsafe** — cost cap, fetch policy, URL rooting, code execution
(the AST sandbox), and the final ship/no-ship verifier. Second, **the LLM already runs a
loop where the task is genuinely open-ended** — `write_query` is a textbook
*evaluator–optimizer* (author a candidate, a deterministic verifier scores it, feed the
diagnosis back, repeat). The crawl is a textbook *constrained agent* (the model steers, code
bounds the frontier and the round budget). The codebase has already discovered the right
patterns; it just hasn't named them or exposed the tools that would let them reach further.

---

## 2. How leading practice frames this choice

The relevant guidance (Anthropic's *Building Effective Agents*, and the same spirit in
OpenAI's practical agent guidance) converges on a few rules that map cleanly onto this task:

- **Find the simplest thing that works; add agency only when it buys you something.** Fixed
  *workflows* give predictability and consistency for well-scoped tasks; *agents* are for
  open-ended problems where you cannot predict the number of steps ahead of time and cannot
  hardcode a path. The recommendation is explicitly to prefer workflows and reserve agents
  for where flexibility and model-driven decisions actually pay off.
- **The winning composite pattern is: an augmented LLM (tools + retrieval + memory) in a
  loop, with a *separate, ideally deterministic, verifier* providing feedback.** The value of
  agents comes precisely from keeping the generator and the checker distinct — an agent that
  grades its own homework is far weaker than one checked by ground truth (tests, a compiler,
  a schema validator).
- **Determinism wins whenever the environment gives you a cheap, trustworthy oracle.** If you
  can *verify* an output programmatically (does the query extract rows? are the required
  fields populated? is the newest row within cadence?), you want that verifier in code, not
  in a prompt — and you want it *outside* the model's control.

Read against those rules, the current design is *more* aligned with best practice than a full
inversion would be. Inverting to a single LLM orchestrator that decides the flow AND writes
the query AND judges success would collapse the generator/verifier separation, surrender the
cost ceiling, and trade a reproducible DAG for a stochastic trajectory — the opposite of the
guidance for a high-volume, auditable, cost-sensitive batch job.

---

## 3. The real trade-offs for *this* task

The task is specific: **onboard many companies for a dataset brief, cheaply, reliably, with
auditable gates.** That profile weights the trade-offs hard toward the workflow:

**Where the fixed pipeline is genuinely superior (keep these):**

- **Cost & token budget.** A fixed pipeline makes ~7–15 LLM calls per company with *bounded*
  prompts (`_clip`, `_FULL_SKELETON`, `_MAX_*_CHARS`). An open-ended agent re-reads a growing
  transcript on every turn; token cost scales with trajectory length, which is exactly what
  you can't predict. `Budget`/`BudgetExceeded` gives a hard USD stop today; an agent loop
  makes spend variance the norm, not the exception. Across N companies this is the dominant
  cost.
- **Determinism & reproducibility.** Re-running a company today produces the same stage
  sequence and the same self-contained query blob. That is what lets you diff runs, cache,
  and trust a re-run. An agent's path is a sample, not a function.
- **Debuggability & traceability.** `result.steps`, per-stage logging, `_summarize`, and the
  `reviews` list give a stage-addressable trace: you know *which stage* failed and why. An
  agent transcript is a haystack; "the crawl review failed: …" is a diagnosis.
- **The gates are load-bearing and must stay deterministic-checked.** `_test_query`,
  `_populated_rows`, `_empty_required_fields`, `_timeliness`, and the flag-driven `write_resolve`
  are ground-truth checks. They are the reason a shipped blob is trustworthy. These must not
  move inside the model's self-judgement — the timeliness gate already *overrides* the LLM
  reviewer on purpose (`review_query` forces `passed=False` when `stale`). Keep that posture.
- **Safety.** `_eval_query_ast` refuses `__globals__`/non-`wq` names so a prompt-injected
  crawled page can't reach code execution. A free-form agent that executes model-authored code
  widens this surface; the sandbox stays essential either way.

**Where the fixed pipeline caps capability (the ceiling argument, which is real):**

- **The long tail of messy sites.** A fixed DAG authors the query **once, blind, from a single
  clipped static skeleton**, then reacts only *after* failure. Sites where the data is behind a
  click, a filter tab, a "load more", a multi-hop drill-down, or a detail page the listing only
  links to — these need *interleaved look-then-act reasoning* that a one-shot author can't do.
  The `_content_hint`/retry machinery is a hand-rolled approximation of exactly the loop an
  agent would run natively, and it's stuck reacting to failures instead of investigating first.
- **Crawl navigation is myopic.** `_pick_edges` chooses from url+link-text only, one round at a
  time, with no ability to peek at a page before committing a fetch or to backtrack a dead
  branch. Real "find the dataset" navigation sometimes needs to open a hub page to see where the
  data actually lives. This is inherently open-ended (you can't predict the depth), which is the
  precise signature of "use an agent here."
- **No cross-stage recovery.** Failures dead-end at a `reason` string. An agent could, e.g., on
  a login wall, go back and try a different candidate or a cached/API path. Today the DAG can't
  loop backward.

The honest synthesis: the ceiling is real but **localised**. It lives in exactly two stages
(query authoring, frontier navigation). Nothing about `search`, `select`, `evaluate`, the
reference/resolve cascade, or the gates benefits from open-ended agency — they benefit from
*determinism*. So the fix is targeted agency, not inversion.

---

## 4. Recommendation: a workflow spine with bounded agents in the two hard stages

Concretely, per stage:

| Stage | Keep deterministic? | Recommendation |
|---|---|---|
| `search_web` | Yes | Stateless LLM classify + fail-open. No change. |
| `crawl_from_seeds` | Mostly | **Modest agency:** let `_pick_edges` optionally request a cheap `peek(url)` (skeleton/title/flags) on 1–2 ambiguous edges before committing, still inside the round/`max_pages` budget. Constrained agent, code-bounded. |
| `select_candidates` | Yes | Stateless ranking + fail-open. No change. |
| `evaluate_candidate` | Yes | Flags stay ground truth. No change. |
| `write_reference` | Yes (pure Python) | No change. |
| `write_resolve` | Yes (pure Python) | No change. |
| **`write_query`** | **No — make it a real bounded agent** | **Highest leverage.** Promote the implicit retry loop to a tool-using loop (below). |
| `review_*` gates | Yes (LLM judge + Python override) | Keep. The deterministic overrides are the point. |

### The highest-leverage change: `write_query` as a bounded, tool-using agent

Today `write_query` is *already* an evaluator–optimizer — it just can't *look* at the page
mid-thought; it authors blind and only learns after a full failed extraction. The tools it
needs **already exist** in the codebase (`tools.py`, `mcp.py`, and the `Document`/`wq` surface):

- `get_skeleton(url|selector)` — `_skeleton_for` / `doc.skeleton(...)` (already used).
- `probe_selector(sel)` — `_selector_match_count` (already written, currently only used in
  hints): *how many records does this match?*
- `get_record_html(sel)` — `_sample_record_html` (already written): *show me one record's raw
  markup so I can see which attribute holds the field.*
- `test_query(code)` — `_parse_query` + `_test_query` + `_populated_rows` +
  `_empty_required_fields` (the deterministic verifier — **stays in code, stays the oracle**).
- `follow_link(sel)` / `render_detail(href)` — for the detail-page and drill-down long tail
  (the guide already documents nested `.resolve()`; the agent should be able to *try* it and
  see the result, not guess it once).

Loop shape: give the model these tools + the schema + the guide, let it **investigate the page
(probe/inspect) before and between authoring attempts**, and stop when the deterministic
verifier says `complete` or a **bounded budget** (max tool calls + `Budget` USD) is hit — then
return the best `QueryArtifact` exactly as today. This is a strict superset of the current
behaviour: at worst it authors in one shot like now; at best it reasons through a messy page
the current one-shot author cannot. Crucially, **the verifier and the ship gate stay in Python
and stay outside the model** — this is the generator/verifier separation the guidance insists
on, preserved.

Why this stage first: (a) it is where the capability ceiling actually bites (the extraction is
the deliverable); (b) the tools and the verifier are *already written* — this is mostly
re-plumbing existing functions into a tool loop, not new capability; (c) it is naturally
bounded (one page, a handful of tools, an existing USD cap), so the cost/latency downside is
contained and measurable.

### What to explicitly NOT do

- **Do not** replace `_onboard_company`'s control flow with an LLM orchestrator. The stage
  order is not the hard part; making it stochastic buys nothing and costs reproducibility,
  the budget ceiling, and the trace.
- **Do not** let the model author the reference or the resolve policy — flags are cheaper and
  more reliable ground truth than a prompt.
- **Do not** let the agent self-certify success. The deterministic verifier + timeliness gate
  is the trust anchor; keep it separate and authoritative.

---

## 5. Migration sketch

**Phase 0 — name what exists (no behaviour change).** Document that `write_query` is an
evaluator–optimizer and the crawl is a constrained agent. Factor the verifier
(`_test_query`+`_populated_rows`+`_empty_required_fields`) into a single
`verify_query(code, doc, brief) -> Verdict` used by both the loop and the gate. This is pure
refactor and de-risks everything after it.

**Phase 1 — turn `write_query` into a bounded tool loop (the main lift).** Expose the five
tools above (all backed by existing functions) to a per-stage agent runner with a hard
tool-call budget and the existing `Budget`. Keep the current one-shot author as the
fallback/first move. Gate the rollout behind a flag and A/B it against the current author on
the overnight brief rotation (the repo already rotates live sites × briefs on a cadence —
ideal eval harness). **Success metric:** completion rate on the messy-site tail and $/company,
held against the deterministic verifier that already exists.

**Phase 2 — give the crawl a peek tool.** Let `_pick_edges` request `peek(url)` on ≤2 edges
per round before committing, within the same `rounds`/`max_pages` bounds. Cheap, bounded,
reversible.

**Phase 3 (optional) — one bounded backtrack.** Allow a single fallback to the next-best
candidate when the chosen source dead-ends (login wall / 0 rows) instead of failing outright.
Still a code-owned loop with a fixed retry count, not open-ended agency.

**Keep unchanged throughout:** the DAG spine, `write_reference`/`write_resolve`, the AST
sandbox, `Budget`, the review gates and their deterministic overrides, and the self-contained
blob output contract.

---

## 6. Bottom line

The current architecture is not behind the state of the art — for a high-volume, cost-capped,
auditable batch task it is *closer* to best practice than a full agentic inversion would be.
The workflow spine, the deterministic gates, and the cost ceiling are genuine strengths, not
legacy timidity. The one place the design leaves capability on the table is that its hardest,
most valuable stage — authoring the extraction — reasons *blind and one-shot* when the task is
inherently *look-then-act*. Promote that stage (and, secondarily, the crawl) from a scripted
retry loop to a **bounded agent with the tools it already has and the verifier it already
trusts**, and you lift the ceiling on the messy-site long tail without giving up a single one
of the properties that make this pipeline shippable at scale.
