# The onboarding pipeline — stages, contracts, state

The pipeline onboards ONE dataset for a brief in nine stages. Every stage has a typed CONTRACT
(a pydantic model it returns), the contracts accumulate in one `Onboarding` STATE that round-trips
to JSON (`state.save(path)` / `Onboarding.load(path)`), and a run RESUMES at the first stage whose
output is missing — so a crashed or budget-stopped run continues where it stopped, and a stage can
be re-run by clearing its output (`state.reset_from("crawl")`).

## Briefs are generic, with arguments

A brief is markdown with YAML frontmatter. It declares `args` (names) and every string in it may
template them with `{name}`: `Brief.load("ir-news").render(company="Intel")`. A missing argument
fails loudly. The `search` section drives stage 1 deterministically: a `term`, `k`, and `domain` /
`path` HINTS — fragments scored against each result's host and path (`investors.{company}`,
`news`, `press`). `look` / `ignore` are the one-line natural-language scope the review stages see.
`schema` is the field list (name, type, description); `optional` and `expect_rows` bound the author.

## LLM calls: `ask(ctx, STAGE, **args)`

Every model call is `ask(ctx, "<stage>", **args)` (or `ask_json(..., Model)` for a typed reply):
`<stage>.md` under `pipeline/prompts/` is a TINY template — one line of goal, the arguments the
stage needs, the reply shape — and the args are brief / state values. No conversations, no
guides, no page dumps: every prompt is bounded by a character budget and the spend is attributed
per stage in `state.spend`. The target is about $0.01 per onboarding on Haiku-class pricing.

## The stages

| # | stage | returns | model calls |
|---|---|---|---|
| 1 | search | `SearchResult` — up to k hits scored by the brief's domain / path hints | 0 |
| 2 | review_search | `SearchReview` — the hits worth following as `must` / `could` / `lead` | 1 |
| 3 | crawl | `CrawlResult` — pages visited, candidates in evaluation order; a `must` is reviewed the moment it is fetched (early stop) | 0–3 |
| 4 | review_candidate | `CandidateReview` — the dataset is on this page (retried once through a browser when the HTTP tier shows nothing) | 1–2 per candidate |
| 5 | expand | `DatasetSource` — everything the author needs: record selector, tier, pagination / API / order / filters / SPA descriptions, from the page's signals | 0 (per-signal explorations later) |
| 6 | review_location | `LocationReview` — the located source summarised against the brief | 1 |
| 7 | author_resolve | `ResolvePlan` — the request that returns the dataset's document (the data API when one exists, at the right tier) | 0 |
| 8 | author_extract | `ExtractQuery` — the field extraction over THAT document; one shot, then repair with precise per-field hints; NO nested resolves | 1–3 |
| 9 | author_review | `AuthorReview` — the final review of the sample against the brief | 1 |

Stage 8 never follows links: a record's detail page is a LATER onboarding of its own (back to stage
7 with the detail URL column as the source, the queries joined) — the seam `AuthorReview.next`
is reserved for it.

## Running

```python
state = Onboarding.start(Brief.load("ir-news"), company="Intel")
ctx = Context(resolver=Resolver(), llm=default_llm(), search=default_search())
await run(state, ctx, save="intel-ir-news.json")   # resumable: re-run the same call after a stop
```

## Cost

Target $0.01 per onboarding; $0.10 is the ceiling. Every prompt input is clipped to
`PROMPT_INPUT_CHARS` (about 700 tokens); the spend is attributed per stage in `state.spend` and
each stage's calls / cost appear in `state.log`. The shim (`claude -p`) pays its own ~18k-token
system prompt per call (about $0.017), so the target is reachable only on the API; the shim stays
a debugging path.

## Learnings carried over (from the old monolith and the loop pipeline)

- Flags (the detection surface) are ground truth; the model judges, it never discovers structure.
- A seed must be HOSTED BY the entity (registrable domain), never a page "about" it; a single
  record (a detail-shaped URL) is never the dataset; the crawl drops detail-shaped leaves and stops
  at the first listing.
- Selector hygiene is mechanical (`web.parse.selectors`): no ids / positions for records, no ids
  for listing fields, generated classes by their stem, `>` relaxed; the skeleton and every hint
  speak the durable form.
- Each page is fetched once per run (the resolve memo); validation runs on a window of records;
  the author never fans out to detail pages.
- The escalation trigger is a REAL block (401/403/429, a challenge page); domain stickiness
  remembers the tier a host needed.
- Record counts are a guide, never a hard rule (`expect_rows` is a range).
- Failure hints must be precise: the closest selectors, the surrounding structure, the available
  attributes / JSON keys, the raw value before a transform emptied it.
- A model error is a retry, never "no data"; every invalid reply is kept in full for a human.
- The verb record (what the model reached for that the DSL lacks) is kept per run.
