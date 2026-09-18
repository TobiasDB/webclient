You are checking a data-onboarding run's authored query RESULT against the brief's EXIT CONDITION. The dataset wanted is: $description.

The brief's EXIT CONDITION (a natural-language check the pipeline is meant to honour): $exit_condition

The authored query extracted $row_count row(s). A sample of the output rows (the actual RESULT data):
$sample

Decide whether the EXIT CONDITION holds when read against the RESULT ABOVE (not against the page — against the extracted rows). For example, if the condition is "the upcoming events section is empty", it HOLDS when the result rows contain no upcoming events.

Be careful: the condition holding on the RESULT can mean one of two things, and you should say which — (a) the source genuinely has no such records (the expected clean exit), or (b) the query MISSED them (e.g. an oddly-formatted single row the record selector did not match). If the result plausibly should contain such records but does not, lean towards (b) and flag it as a possible miss.

Reply with ONLY a JSON object: "met" (bool: true if the exit condition holds on the RESULT), "likely_missed" (bool: true if the condition holds only because the query probably failed to capture records that exist), "reason" (one sentence a human reads).
