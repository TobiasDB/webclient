You are reviewing the SELECT stage of a data-onboarding run. The goal is this dataset: $description.$fields_line

All crawled pages:
$pages

The pages selected as candidates to evaluate (url [tier] note):
$candidates

The source finally chosen to scrape: $chosen

Judge the selection:
- Were the RIGHT URLs picked as candidates — the ones that actually hold the dataset — and not chrome/legal/marketing pages?
- Was a better candidate present in the crawled pages but NOT selected (a missed listing / data endpoint)?
- Was the best available source chosen to scrape, given the goal?

This review GATES the pipeline: if the wrong source was chosen (or the right one wasn't among the candidates), the run should stop rather than author a query against the wrong page.

Reply with ONLY a JSON object: "pass" (bool: true if the selection is good enough to continue; false to FAIL the run), "verdict" ("good"|"partial"|"poor"), "score" (0-10), "issues" (a list of short concrete problems, [] if none), "summary" (one sentence a human reads).
