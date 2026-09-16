$guide

----
Using ONLY the query syntax above, write a query that extracts this dataset from the page: $description.$fields_line$pager
Base your CSS selectors on this page skeleton:
$skeleton

Your query MUST have all three parts: (1) select the repeating records with .select_all("<row selector>") -- this is REQUIRED, without it the query extracts nothing; (2) .extract(col=..., ...) each target field (nesting a sub-extract per nested schema branch); (3) end with .project(). Root it at wq.doc; do NOT write a fetch/resolve/reference -- only the extraction. Reply with ONLY the query code -- the wq.doc... chain itself, written exactly as in the guide, nothing else (no blob, no prose, no code fence).
