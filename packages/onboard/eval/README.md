# onboard eval

Runs the two onboarding phases against the **webclient lab** (the OLD repo's `webclient/lab/`
fixtures, each publishing its expected result) and grades every example:

- **Locate** (`web.onboard.locate`) -- find the right source: the expected record selector, or a
  JSON/XML data document, preferring a consistent XHR/data-API when one backs the page.
- **Author** (`web.onboard.author` -> `build_query`) -- write a `wq` query and RUN it, grading the
  rows against the fixture's expected result.

Each case is graded **PASS / PARTIAL / FAIL**. The committed [`RESULTS.md`](RESULTS.md) is the
latest run.

## The Author LLM

Author needs a model. If `ANTHROPIC_API_KEY` is set, swap `HeuristicLlm` for
`web.onboard.AnthropicLlm` in `harness.run_case`. With **no key** (the default here), the eval uses
`HeuristicLlm` -- a deterministic stand-in that reads the prompt's skeleton + fields and writes the
query by a fixed heuristic. It therefore measures the **pipeline mechanics** (prompt -> parse ->
reroot -> run) and how far a mechanical author gets, **not** a real model's selector quality.

## Running

Both `web.onboard` and `webclient` (for the lab) must be importable. From `packages/onboard`:

```
python -m eval
```

This serves the lab on a random localhost port (pure stdlib, a daemon thread), runs every case,
prints the table, and rewrites `RESULTS.md`.

## Layout

- `heuristic_llm.py` -- the deterministic author stand-in.
- `harness.py` -- the `Case` list, the Locate/Author run, and the grader.
- `__main__.py` -- serve the lab, run, print, write `RESULTS.md`.
- `RESULTS.md` -- the committed latest run.
