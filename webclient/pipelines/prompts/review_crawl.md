You are reviewing the CRAWL stage of a data-onboarding run. The goal was to reach the pages that hold this dataset: $description.$fields_line

Seed URLs the crawl started from:
$seeds

Pages the crawl actually fetched (url [tier; flags] — title):
$pages

Judge the crawl's choices:
- Did it reach the page(s) most likely to HOLD the dataset (a listing / data endpoint / the relevant section), or did it wander off into irrelevant pages?
- Was it COMPLETE for this goal — did it likely miss an obvious better source (e.g. a press-release/news listing, an export/API endpoint) that a human would have gone to?
- Was it too shallow (stopped short) or too broad (spent its budget on chrome)?

Reply with ONLY a JSON object: "verdict" ("good"|"partial"|"poor"), "score" (0-10), "issues" (a list of short concrete problems, [] if none), "summary" (one sentence a human reads).
