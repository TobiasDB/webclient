You are reviewing the QUERY stage of a data-onboarding run. The dataset wanted is: $description.$fields_line

The authored query (its selectors and shape):
$query

It extracted $row_count row(s) (tested against the page: $tested). A sample of the output rows:
$sample

The page it runs against (skeleton — an outline of the real DOM):
$skeleton

Timeliness of the data (is the newest row recent, given how often rows appear?):
$timeliness

Judge the query against the page and the brief. What matters here is CORRECTNESS and TIMELINESS:
- CORRECTNESS: does the OUTPUT TABLE hold data that matches the brief's schema — the right fields, filled with real values (not blanks, not the wrong thing)? Are the SELECTORS targeting the RELEVANT record/field elements in the skeleton, not something incidental? Are any required fields empty or mis-mapped?
- TIMELINESS: for an ongoing/current dataset (news, releases, listings), the MOST RECENT items must be present. If the timeliness check above says the newest item is far older than the typical interval between rows, the latest data is MISSING (it is probably client-rendered or behind a tab/filter that wasn't captured) — this must FAIL, even with many older rows.
$completeness

This review GATES the pipeline: a query whose output does not match the brief (wrong/empty fields, selectors that miss the real records) or is NOT TIMELY (the most recent data is missing) should FAIL the run even if it technically ran, so we don't ship a wrong or stale dataset.

Reply with ONLY a JSON object: "pass" (bool: true if the output genuinely matches the brief and is complete; false to FAIL the run), "verdict" ("good"|"partial"|"poor"), "score" (0-10), "issues" (a list of short concrete problems, [] if none), "summary" (one sentence a human reads).
