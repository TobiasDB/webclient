You are reviewing the QUERY stage of a data-onboarding run. The dataset wanted is: $description.$fields_line

The authored query (its selectors and shape):
$query

It extracted $row_count row(s) (tested against the page: $tested). A sample of the output rows:
$sample

The page it runs against (skeleton — an outline of the real DOM):
$skeleton

Judge the query against the page and the brief:
- Does the OUTPUT TABLE hold data that matches the brief's schema — the right fields, filled with real values (not blanks, not the wrong thing)?
- Are the SELECTORS targeting the RELEVANT parts of the page (the actual record/field elements in the skeleton), or something incidental?
- Do they capture ALL the records on the page — is the row_count plausibly the FULL set, or did the query miss rows (too-narrow a record selector, a missed container, pagination not followed)?
- Are any required fields empty, mis-mapped, or missing?

This review GATES the pipeline: a query whose output does not actually match the brief -- wrong/empty fields, selectors that miss the real records, or only a fraction of the rows -- should FAIL the run even if it technically ran, so we don't ship a wrong dataset.

Reply with ONLY a JSON object: "pass" (bool: true if the output genuinely matches the brief and is complete; false to FAIL the run), "verdict" ("good"|"partial"|"poor"), "score" (0-10), "issues" (a list of short concrete problems, [] if none), "summary" (one sentence a human reads).
