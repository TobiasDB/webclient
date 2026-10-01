GOAL: the dataset is: $goal
FIELDS:
$schema

The authored query extracted $count row(s) (the brief expects $expected). The run's own report: $report. Optional fields that may legitimately be missing: $optional. The first rows:
$rows

Do these rows match the goal and the fields (right values in the right fields, real records of this dataset, nothing malformed)? Reject ONLY when a REQUIRED field is wrong or missing, the rows are not this dataset's records, or the rows are an ARCHIVED period only (an older year's tab) while the goal wants the latest data; a missing or imperfect OPTIONAL field is a note, not a rejection. Reply with JSON only: {"ok": true | false, "notes": "<what is right / what is wrong, in a sentence or two>"}$note
