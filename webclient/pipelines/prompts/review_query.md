You are reviewing the QUERY stage of a data-onboarding run. The dataset wanted is: $description.$fields_line

The authored query (its selectors and shape):
$query

It extracted $row_count row(s) (tested against the page: $tested). A sample of the output rows:
$sample

The page it runs against (skeleton — an outline of the real DOM):
$skeleton

Completeness of the most-recent data:
$recency

Judge the query against the page and the brief — CORRECTNESS and COMPLETENESS matter equally:
- Does the OUTPUT TABLE hold data that matches the brief's schema — the right fields, filled with real values (not blanks, not the wrong thing)?
- Are the SELECTORS targeting the RELEVANT parts of the page (the actual record/field elements in the skeleton), or something incidental?
- Do they capture ALL the records — is the row_count plausibly the FULL set, or did the query miss rows (too-narrow a record selector, a missed container, pagination/tabs not followed)?
- COMPLETENESS OF RECENT DATA: for an ongoing/current dataset (news, releases, listings), the MOST RECENT items must be present. If the recency check above says newer items are missing (e.g. the newest row is from a past year while today is later), the dataset is INCOMPLETE — the current data is probably client-rendered or behind a tab/filter that wasn't captured — and this must FAIL, even with many historic rows.
- Are any required fields empty, mis-mapped, or missing?

This review GATES the pipeline: a query whose output does not actually match the brief -- wrong/empty fields, selectors that miss the real records, only a fraction of the rows, or MISSING THE MOST RECENT DATA -- should FAIL the run even if it technically ran, so we don't ship a wrong or stale dataset.

Reply with ONLY a JSON object: "pass" (bool: true if the output genuinely matches the brief and is complete; false to FAIL the run), "verdict" ("good"|"partial"|"poor"), "score" (0-10), "issues" (a list of short concrete problems, [] if none), "summary" (one sentence a human reads).
